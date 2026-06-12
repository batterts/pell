package dev.pell.intellij.debugger

import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.intellij.execution.process.NopProcessHandler
import com.intellij.execution.ui.ConsoleViewContentType
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.fileTypes.FileType
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.xdebugger.XDebugProcess
import com.intellij.xdebugger.XDebugSession
import com.intellij.xdebugger.XDebuggerUtil
import com.intellij.xdebugger.XSourcePosition
import com.intellij.xdebugger.breakpoints.XBreakpointHandler
import com.intellij.xdebugger.breakpoints.XBreakpointProperties
import com.intellij.xdebugger.breakpoints.XLineBreakpoint
import com.intellij.xdebugger.evaluation.XDebuggerEditorsProvider
import com.intellij.xdebugger.frame.XCompositeNode
import com.intellij.xdebugger.frame.XExecutionStack
import com.intellij.xdebugger.frame.XNamedValue
import com.intellij.xdebugger.frame.XStackFrame
import com.intellij.xdebugger.frame.XSuspendContext
import com.intellij.xdebugger.frame.XValueChildrenList
import com.intellij.xdebugger.frame.XValueNode
import com.intellij.xdebugger.frame.XValuePlace
import dev.pell.intellij.run.PellRunConfiguration
import java.io.BufferedReader
import java.io.File
import java.io.OutputStreamWriter
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * SQL-transport debug engine: drives `pell debug-serve` (DBMS_DEBUG —
 * the classic Probe API) over JSON lines on stdio. Everything is an
 * OUTBOUND database connection: no JDWP listener, no network ACE, no
 * inbound path — if `pell exec` works through your tunnel, this does.
 *
 * Selected with PELL_DEBUG_TRANSPORT=sql (default stays jdwp).
 * Limits vs jdwp: breakpoints bind to deployed packages (set
 * breakpoints in module .pell files; in the script itself, step from
 * the start instead — Probe can't address anonymous blocks by name).
 */
class PellSqlDebugProcess(
    session: XDebugSession,
    private val config: PellRunConfiguration,
) : XDebugProcess(session) {

    private val project: Project = session.project
    private val pellExe: File? = PellCli.resolve(config, project)
    private val workDir: File = pellExe?.parentFile ?: File(project.basePath ?: ".")
    private val dummyHandler = NopProcessHandler()
    private val sqlHighlight = PellSqlExecutionMarker(project)
    // owner.name -> deployed PL/SQL source (ALL_SOURCE), line N = unit line N
    private val deployedSource = ConcurrentHashMap<String, com.intellij.testFramework.LightVirtualFile>()

    @Volatile private var serve: Process? = null
    private var stdin: OutputStreamWriter? = null
    @Volatile private var stopped = false

    @Volatile private var scriptMap: PellSrcMap? = null
    private val moduleMaps = ConcurrentHashMap<String, PellSrcMap>()

    // request/response: locals replies land in this future
    @Volatile private var localsFuture: CompletableFuture<List<Pair<String, String?>>>? = null

    // Active pell-granular step: keep re-sending the step command while
    // the suspend event maps to the SAME pell line (pell statements span
    // many PL/SQL rows). Unmapped (foreign) frames surface raw steps.
    private data class ActiveStep(val cmd: String, val origin: Pair<String, Int>?,
                                  var hops: Int = 0)
    @Volatile private var activeStep: ActiveStep? = null
    @Volatile private var lastSuspendPell: Pair<String, Int>? = null
    private val bpById = ConcurrentHashMap<String, XLineBreakpoint<*>>()
    private var bpCounter = 0
    private val queuedBreakpoints = mutableListOf<XLineBreakpoint<*>>()
    @Volatile private var ready = false
    @Volatile private var started = false   // first run sent

    private val breakpointHandler = object :
        XBreakpointHandler<XLineBreakpoint<XBreakpointProperties<*>>>(PellLineBreakpointType::class.java) {
        override fun registerBreakpoint(bp: XLineBreakpoint<XBreakpointProperties<*>>) {
            ApplicationManager.getApplication().executeOnPooledThread { addBreakpoint(bp) }
        }
        override fun unregisterBreakpoint(bp: XLineBreakpoint<XBreakpointProperties<*>>, temporary: Boolean) {
            // Probe breakpoint deletion needs the bp#; v1 keeps it simple —
            // removed breakpoints stop being reported (we just won't match).
        }
    }

    override fun getBreakpointHandlers(): Array<XBreakpointHandler<*>> = arrayOf(breakpointHandler)

    override fun getEditorsProvider(): XDebuggerEditorsProvider = object : XDebuggerEditorsProvider() {
        override fun getFileType(): FileType = dev.pell.intellij.PellFileType
        override fun createDocument(
            project: Project, expression: String,
            sourcePosition: XSourcePosition?, mode: com.intellij.xdebugger.evaluation.EvaluationMode,
        ) = com.intellij.openapi.editor.EditorFactory.getInstance().createDocument(expression)
    }

    override fun doGetProcessHandler() = dummyHandler

    override fun sessionInitialized() {
        ApplicationManager.getApplication().executeOnPooledThread { start() }
    }

    private fun console(text: String, type: ConsoleViewContentType = ConsoleViewContentType.SYSTEM_OUTPUT) {
        session.consoleView?.print(text + "\n", type)
    }

    private fun start() {
        try {
            val exe = pellExe ?: error("pell CLI not found")
            val script = config.filePath ?: error("run configuration has no file")

            val deployPath = config.options.deployPath ?: script
            console("pell debugger (sql transport): deploying $deployPath with --debug ...")
            val env = PellCli.env(config, project)
            val deploy = PellCli.run(exe, workDir, "deploy", deployPath, "--debug",
                                     "--out-dir", File(workDir, ".pell-debug/out").absolutePath,
                                     extraEnv = env)
            if (deploy.first != 0) {
                console("deploy --debug failed (continuing):\n${deploy.second}",
                        ConsoleViewContentType.ERROR_OUTPUT)
            }
            val anon = PellCli.run(exe, workDir, "srcmap", script, "--anon")
            if (anon.first == 0) scriptMap = PellSrcMap.parse(anon.second)
            config.options.deployPath?.let { dep ->
                if (File(dep).isFile) {
                    val m = PellCli.run(exe, workDir, "srcmap", dep)
                    if (m.first == 0) moduleMaps[File(dep).canonicalPath] = PellSrcMap.parse(m.second)
                }
            }

            val servePb = ProcessBuilder(exe.absolutePath, "debug-serve", script)
                .directory(workDir)
                .redirectErrorStream(false)
            servePb.environment().putAll(env)
            val p = servePb.start()
            serve = p
            stdin = OutputStreamWriter(p.outputStream, Charsets.UTF_8)
            Thread({ p.errorStream.bufferedReader().forEachLine { /* diagnostics */ } },
                   "pell-serve-stderr").apply { isDaemon = true; start() }
            eventLoop(p.inputStream.bufferedReader())
        } catch (e: Exception) {
            if (!stopped) {
                console("pell debugger failed: ${e.message}", ConsoleViewContentType.ERROR_OUTPUT)
                ApplicationManager.getApplication().invokeLater { session.stop() }
            }
        }
    }

    private fun send(vararg pairs: Pair<String, Any?>) {
        val o = JsonObject()
        for ((k, v) in pairs) when (v) {
            null -> o.add(k, com.google.gson.JsonNull.INSTANCE)
            is Number -> o.addProperty(k, v)
            else -> o.addProperty(k, v.toString())
        }
        synchronized(this) {
            stdin?.apply { write(o.toString() + "\n"); flush() }
        }
    }

    private fun eventLoop(reader: BufferedReader) {
        for (line in reader.lineSequence()) {
            if (stopped) break
            val ev = runCatching { JsonParser.parseString(line).asJsonObject }.getOrNull() ?: continue
            when (ev.get("event")?.asString) {
                "ready" -> {
                    ready = true
                    synchronized(queuedBreakpoints) {
                        queuedBreakpoints.forEach { sendBreakpoint(it) }
                        queuedBreakpoints.clear()
                    }
                    console("pell debugger: attached (probe session " +
                            ev.get("debug_session_id").asString.take(12) + "…)")
                    send("cmd" to "run")
                    started = true
                }
                "bp_set" -> {
                    val ok = ev.get("status").asInt == 0
                    val bp = ev.get("id")?.asString?.let { bpById[it] }
                    if (bp != null) {
                        ApplicationManager.getApplication().invokeLater {
                            if (ok) session.updateBreakpointPresentation(
                                bp, com.intellij.icons.AllIcons.Debugger.Db_verified_breakpoint, null)
                            else session.updateBreakpointPresentation(
                                bp, com.intellij.icons.AllIcons.Debugger.Db_invalid_breakpoint,
                                "Probe rejected this breakpoint (status ${ev.get("status").asInt})")
                        }
                    }
                }
                "suspended" -> {
                    val bp = matchBreakpoint(ev)
                    val step = activeStep
                    if (bp == null && step != null) {
                        val cur = pellPosOf(ev)
                        if (cur != null && cur == step.origin && step.hops < 256) {
                            step.hops++
                            send("cmd" to step.cmd)
                            continue
                        }
                    }
                    activeStep = null
                    lastSuspendPell = pellPosOf(ev)
                    val ctx = buildContext(ev)
                    updateSqlHighlight(ev)
                    ApplicationManager.getApplication().invokeLater {
                        if (bp != null) session.breakpointReached(bp, null, ctx)
                        else session.positionReached(ctx)
                    }
                }
                "locals" -> {
                    val vals = ev.getAsJsonArray("values").map { v ->
                        val o = v.asJsonObject
                        o.get("name").asString to
                            o.get("value")?.takeIf { !it.isJsonNull }?.asString
                    }
                    localsFuture?.complete(vals)
                }
                "terminated" -> {
                    ev.getAsJsonArray("output")?.forEach {
                        console(it.asString, ConsoleViewContentType.NORMAL_OUTPUT)
                    }
                    val err = ev.get("error")?.takeIf { !it.isJsonNull }?.asString
                    if (err != null) console("error: $err", ConsoleViewContentType.ERROR_OUTPUT)
                    break
                }
                "error" -> console("pell debugger: ${ev.get("message").asString}",
                                   ConsoleViewContentType.ERROR_OUTPUT)
            }
        }
        if (!stopped) {
            stopped = true
            ApplicationManager.getApplication().invokeLater {
                dummyHandler.destroyProcess()
                session.stop()
            }
        }
    }

    // ------------------------------------------------------------------

    private fun addBreakpoint(bp: XLineBreakpoint<*>) {
        if (!ready) {
            synchronized(queuedBreakpoints) {
                if (!ready) { queuedBreakpoints.add(bp); return }
            }
        }
        sendBreakpoint(bp)
    }

    private fun sendBreakpoint(bp: XLineBreakpoint<*>) {
        val pos = bp.sourcePosition ?: return
        val filePath = pos.file.canonicalPath ?: return
        val pellLine = pos.line + 1
        val map = mapForFile(filePath) ?: run {
            console("pell debugger: no line map for ${pos.file.name}",
                    ConsoleViewContentType.ERROR_OUTPUT)
            return
        }
        val target = map.resolveBreakpoint(pellLine) ?: return
        val unit = target.first
        if (unit.kind == "BLOCK") {
            console("pell debugger (sql transport): breakpoints in the script itself " +
                    "aren't supported by Probe — set them in the module, or step from the top.",
                    ConsoleViewContentType.ERROR_OUTPUT)
            return
        }
        val id = "bp${++bpCounter}"
        bpById[id] = bp
        send("cmd" to "set_breakpoint",
             "owner" to unit.schema,
             "name" to unit.name,
             "namespace" to 2,  // pkg + type bodies share Probe namespace 2
             "line" to target.second,
             "id" to id)
    }

    private fun mapForFile(filePath: String): PellSrcMap? {
        val canonical = File(filePath).canonicalPath
        if (canonical == config.filePath?.let { File(it).canonicalPath }) return scriptMap
        moduleMaps[canonical]?.let { return it }
        val exe = pellExe ?: return null
        val r = PellCli.run(exe, workDir, "srcmap", canonical)
        if (r.first != 0) return null
        return PellSrcMap.parse(r.second).also { moduleMaps[canonical] = it }
    }

    private fun matchBreakpoint(ev: JsonObject): XLineBreakpoint<*>? {
        if (ev.get("reason")?.asString != "breakpoint") return null
        val name = ev.get("name")?.takeIf { !it.isJsonNull }?.asString ?: return null
        val line = ev.get("line")?.takeIf { !it.isJsonNull }?.asInt ?: return null
        return bpById.values.firstOrNull { bp ->
            val pos = bp.sourcePosition ?: return@firstOrNull false
            val map = pos.file.canonicalPath?.let { mapForFile(it) } ?: return@firstOrNull false
            val u = map.units.firstOrNull { it.name.equals(name, true) && it.kind != "BLOCK" }
            u?.unitLineForPell(pos.line + 1) == line
        }
    }

    private fun buildContext(ev: JsonObject): XSuspendContext {
        data class F(val owner: String?, val name: String?, val line: Int)
        val frames = ev.getAsJsonArray("stack")?.map {
            val o = it.asJsonObject
            F(o.get("owner")?.takeIf { x -> !x.isJsonNull }?.asString,
              o.get("name")?.takeIf { x -> !x.isJsonNull }?.asString,
              o.get("line")?.takeIf { x -> !x.isJsonNull }?.asInt ?: -1)
        } ?: emptyList()

        val xframes = frames.mapIndexed { i, f ->
            object : XStackFrame() {
                override fun getSourcePosition(): XSourcePosition? {
                    if (f.name == null) {
                        // anon block frame -> the script file
                        val pellLine = scriptMap?.units?.firstOrNull()?.pellLineForUnit(f.line)
                            ?: return null
                        val vf = LocalFileSystem.getInstance()
                            .findFileByPath(config.filePath ?: return null) ?: return null
                        return XDebuggerUtil.getInstance().createPosition(vf, pellLine - 1)
                    }
                    for ((path, map) in moduleMaps) {
                        val u = map.units.firstOrNull {
                            it.name.equals(f.name, true) && it.kind == "PACKAGE BODY"
                        } ?: continue
                        val pellLine = u.pellLineForUnit(f.line) ?: continue
                        val vf = LocalFileSystem.getInstance().findFileByPath(path) ?: continue
                        return XDebuggerUtil.getInstance().createPosition(vf, pellLine - 1)
                    }
                    return null
                }

                override fun customizePresentation(c: com.intellij.ui.ColoredTextContainer) {
                    val label = if (f.name == null) "(script):${f.line}"
                                else "${f.owner ?: ""}.${f.name}:${f.line}"
                    c.append(label, com.intellij.ui.SimpleTextAttributes.REGULAR_ATTRIBUTES)
                    c.setIcon(com.intellij.icons.AllIcons.Debugger.Frame)
                }

                override fun computeChildren(node: XCompositeNode) {
                    if (i != 0) { node.addChildren(XValueChildrenList.EMPTY, true); return }
                    ApplicationManager.getApplication().executeOnPooledThread {
                        val fut = CompletableFuture<List<Pair<String, String?>>>()
                        localsFuture = fut
                        send("cmd" to "locals", "frame" to 0)
                        val vals = runCatching { fut.get(15, TimeUnit.SECONDS) }
                            .getOrDefault(emptyList())
                        val children = XValueChildrenList()
                        for ((n, v) in vals) {
                            children.add(object : XNamedValue(n) {
                                override fun computePresentation(node: XValueNode, place: XValuePlace) {
                                    node.setPresentation(
                                        com.intellij.icons.AllIcons.Debugger.Value, null,
                                        v ?: "null", false)
                                }
                            })
                        }
                        node.addChildren(children, true)
                    }
                }
            }
        }
        return object : XSuspendContext() {
            private val stack = object : XExecutionStack("Oracle session (sql transport)") {
                override fun getTopFrame(): XStackFrame? = xframes.firstOrNull()
                override fun computeStackFrames(firstFrameIndex: Int, container: XStackFrameContainer?) {
                    container?.addStackFrames(xframes.drop(firstFrameIndex), true)
                }
            }
            override fun getActiveExecutionStack(): XExecutionStack = stack
        }
    }

    /** Pell (file, line) of a suspend event, or null when unmapped. */
    private fun pellPosOf(ev: JsonObject): Pair<String, Int>? {
        val name = ev.get("name")?.takeIf { !it.isJsonNull }?.asString
        val line = ev.get("line")?.takeIf { !it.isJsonNull }?.asInt ?: return null
        if (name == null) {
            val pellLine = scriptMap?.units?.firstOrNull()?.pellLineForUnit(line) ?: return null
            return (config.filePath ?: return null) to pellLine
        }
        for ((path, map) in moduleMaps) {
            val u = map.units.firstOrNull {
                it.name.equals(name, true) && it.kind == "PACKAGE BODY"
            } ?: continue
            val pellLine = u.pellLineForUnit(line) ?: continue
            return path to pellLine
        }
        return null
    }

    private fun beginStep(cmd: String) {
        sqlHighlight.clear()
        activeStep = ActiveStep(cmd, lastSuspendPell)
        send("cmd" to cmd)
    }

    override fun startStepInto(context: XSuspendContext?) = beginStep("step_into")
    override fun startStepOut(context: XSuspendContext?) = beginStep("step_out")
    override fun startStepOver(context: XSuspendContext?) = beginStep("step_over")
    override fun resume(context: XSuspendContext?) {
        activeStep = null
        sqlHighlight.clear()
        send("cmd" to "continue")
    }

    /** Paired execution marker in the deployed PL/SQL for the suspended
     *  unit line (package/type bodies; the anon block has no .sql). */
    private fun updateSqlHighlight(ev: JsonObject) {
        val owner = ev.get("owner")?.takeIf { !it.isJsonNull }?.asString
        val name = ev.get("name")?.takeIf { !it.isJsonNull }?.asString
        val line = ev.get("line")?.takeIf { !it.isJsonNull }?.asInt
        if (name == null || line == null) { sqlHighlight.clear(); return }
        val vf = deployedSourceFile(owner, name) ?: run { sqlHighlight.clear(); return }
        sqlHighlight.show(vf, (line - 1).coerceAtLeast(0))
    }

    private fun deployedSourceFile(owner: String?, name: String): com.intellij.testFramework.LightVirtualFile? {
        val unit = if (owner != null) "$owner.$name" else name
        deployedSource[unit]?.let { return it }
        val exe = pellExe ?: return null
        val r = PellCli.run(exe, workDir, "debug-source", unit, "--type", "PACKAGE_BODY",
                            extraEnv = PellCli.env(config, project))
        if (r.first != 0) return null
        val vf = com.intellij.testFramework.LightVirtualFile("${unit}.sql", r.second)
        vf.isWritable = false
        deployedSource[unit] = vf
        return vf
    }

    override fun stop() {
        stopped = true
        sqlHighlight.clear()
        runCatching { send("cmd" to "stop") }
        ApplicationManager.getApplication().executeOnPooledThread {
            val p = serve ?: return@executeOnPooledThread
            if (!p.waitFor(10, TimeUnit.SECONDS)) p.destroyForcibly()
        }
    }
}

/** Shared pell CLI resolution + invocation. */
object PellCli {
    /** Process environment for spawned pell tools: run-config env vars
     *  win, then the settings-page DB URL, then the IDE's inherited
     *  environment (ProcessBuilder supplies that part itself). */
    fun env(config: PellRunConfiguration, project: Project): Map<String, String> {
        val out = LinkedHashMap<String, String>()
        val dbUrl = dev.pell.intellij.settings.PellSettings.getInstance(project).dbUrl
        if (dbUrl.isNotBlank()) out["PELL_DB_URL"] = dbUrl
        out.putAll(config.envVars)
        return out
    }

    fun resolve(config: PellRunConfiguration, project: Project): File? {
        config.pellHome?.takeIf { it.isNotBlank() }?.let { home ->
            File(home, "pell").takeIf { it.canExecute() }?.let { return it }
            File(home).takeIf { it.canExecute() }?.let { return it }
        }
        project.basePath?.let { root ->
            File(root, "pell").takeIf { it.canExecute() }?.let { return it }
        }
        for (dir in (System.getenv("PATH") ?: "").split(File.pathSeparator)) {
            if (dir.isBlank()) continue
            File(dir, "pell").takeIf { it.canExecute() }?.let { return it }
        }
        return null
    }

    fun run(exe: File, workDir: File, vararg args: String,
            extraEnv: Map<String, String> = emptyMap()): Pair<Int, String> = try {
        val pb = ProcessBuilder(listOf(exe.absolutePath) + args)
            .directory(workDir)
            .redirectErrorStream(true)
        pb.environment().putAll(extraEnv)
        val p = pb.start()
        val out = p.inputStream.bufferedReader().readText()
        p.waitFor(180, TimeUnit.SECONDS)
        p.exitValue() to out
    } catch (e: Exception) {
        -1 to (e.message ?: "process failed")
    }
}
