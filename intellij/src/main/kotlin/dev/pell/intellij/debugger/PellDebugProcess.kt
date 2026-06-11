package dev.pell.intellij.debugger

import com.intellij.execution.configurations.GeneralCommandLine
import com.intellij.execution.process.OSProcessHandler
import com.intellij.execution.process.ProcessHandler
import com.intellij.execution.ui.ConsoleViewContentType
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.fileTypes.FileType
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.testFramework.LightVirtualFile
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
import com.sun.jdi.Bootstrap
import com.sun.jdi.ObjectReference
import com.sun.jdi.PrimitiveValue
import com.sun.jdi.StackFrame
import com.sun.jdi.StringReference
import com.sun.jdi.ThreadReference
import com.sun.jdi.VMDisconnectedException
import com.sun.jdi.VirtualMachine
import com.sun.jdi.connect.ListeningConnector
import com.sun.jdi.event.BreakpointEvent
import com.sun.jdi.event.ClassPrepareEvent
import com.sun.jdi.event.EventSet
import com.sun.jdi.event.StepEvent
import com.sun.jdi.event.VMDeathEvent
import com.sun.jdi.event.VMDisconnectEvent
import com.sun.jdi.request.BreakpointRequest
import com.sun.jdi.request.EventRequest
import com.sun.jdi.request.StepRequest
import dev.pell.intellij.run.PellRunConfiguration
import java.io.File
import java.net.DatagramSocket
import java.net.InetAddress
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The pell debugger engine: bridges IntelliJ's XDebugger UI to the
 * Oracle session debugging itself over JDWP (DBMS_DEBUG_JDWP — the
 * SQL Developer mechanism, driven here through JDI).
 *
 * Lifecycle (all JDI work on pooled threads — never the EDT):
 *
 *   1. `pell deploy <deployPath> --debug` — markers + PLSQL_DEBUG units
 *   2. `pell srcmap` (module) + `pell srcmap --anon` (the script) — the
 *      pell↔unit line maps
 *   3. JDI SocketListen; spawn `pell debug-target <script> --jdwp
 *      host:port --wait-for-go` (console = its stdout)
 *   4. accept() the database's connection; arm queued breakpoints
 *      (deferred to ClassPrepare for units not yet loaded)
 *   5. write the "go" line — only then does the block start, so first-
 *      statement breakpoints can't be outrun
 *   6. event loop: breakpoints/steps → XDebugger suspend contexts;
 *      frames in pell units map back through the srcmap; frames in
 *      foreign PL/SQL fetch ALL_SOURCE via `pell debug-source` into
 *      read-only editors — stepping continues through PL/SQL when
 *      pell source runs out.
 *
 * Protocol quirks this code relies on are pinned by
 * OracleJdwpProtocolTest — see that test before changing behavior.
 */
class PellDebugProcess(
    session: XDebugSession,
    private val config: PellRunConfiguration,
) : XDebugProcess(session) {

    private val project: Project = session.project
    private val pellExe: File? = resolvePell()
    private val workDir: File = pellExe?.parentFile ?: File(project.basePath ?: ".")

    private var processHandler: OSProcessHandler? = null
    @Volatile private var vm: VirtualMachine? = null
    private var connector: ListeningConnector? = null
    private var connectorArgs: MutableMap<String, com.sun.jdi.connect.Connector.Argument>? = null

    private val vmAttached = CountDownLatch(1)
    private val goSent = AtomicBoolean(false)
    @Volatile private var stopped = false

    // srcmaps: script (anon block) + one per module file we know about
    @Volatile private var scriptMap: PellSrcMap? = null
    private val moduleMaps = ConcurrentHashMap<String, PellSrcMap>()   // file path -> map

    // breakpoints not yet armed (their unit class isn't loaded yet):
    // jdwp matcher -> (unit line, xbreakpoint)
    private data class Pending(val map: PellSrcMap.Unit, val unitLine: Int,
                               val bp: XLineBreakpoint<*>)
    private val pending = mutableListOf<Pending>()
    private val armed = ConcurrentHashMap<BreakpointRequest, XLineBreakpoint<*>>()

    // fetched PL/SQL source for foreign frames: class name -> (file, text)
    private val foreignSource = ConcurrentHashMap<String, LightVirtualFile>()

    @Volatile private var currentEventSet: EventSet? = null
    @Volatile private var currentThread: ThreadReference? = null

    private val breakpointHandler = object :
        XBreakpointHandler<XLineBreakpoint<XBreakpointProperties<*>>>(PellLineBreakpointType::class.java) {
        override fun registerBreakpoint(bp: XLineBreakpoint<XBreakpointProperties<*>>) {
            ApplicationManager.getApplication().executeOnPooledThread { addBreakpoint(bp) }
        }
        override fun unregisterBreakpoint(bp: XLineBreakpoint<XBreakpointProperties<*>>, temporary: Boolean) {
            ApplicationManager.getApplication().executeOnPooledThread { removeBreakpoint(bp) }
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

    override fun doGetProcessHandler(): ProcessHandler? = processHandler

    override fun sessionInitialized() {
        ApplicationManager.getApplication().executeOnPooledThread { startDebugSession() }
    }

    // ------------------------------------------------------------------
    // startup
    // ------------------------------------------------------------------

    private fun console(text: String, type: ConsoleViewContentType = ConsoleViewContentType.SYSTEM_OUTPUT) {
        session.consoleView?.print(text + "\n", type)
    }

    private fun startDebugSession() {
        try {
            val exe = pellExe ?: error("pell CLI not found — set pell home on the run config")
            val script = config.filePath ?: error("run configuration has no file")

            // 1. debug-deploy the module under test (best-effort: the
            // script itself may be a plain exec script, not a module).
            val deployPath = config.options.deployPath ?: script
            console("pell debugger: deploying $deployPath with --debug ...")
            val deploy = runPell(exe, "deploy", deployPath, "--debug",
                                 "--out-dir", File(workDir, ".pell-debug/out").absolutePath)
            if (deploy.first != 0) {
                console("deploy --debug failed (continuing; modules may already be deployed):\n${deploy.second}",
                        ConsoleViewContentType.ERROR_OUTPUT)
            }

            // 2. line maps.
            val anonMap = runPell(exe, "srcmap", script, "--anon")
            if (anonMap.first == 0) scriptMap = PellSrcMap.parse(anonMap.second)
            config.options.deployPath?.let { dep ->
                if (File(dep).isFile) {
                    val m = runPell(exe, "srcmap", dep)
                    if (m.first == 0) moduleMaps[File(dep).canonicalPath] = PellSrcMap.parse(m.second)
                }
            }

            // 3. listen + launch the target.
            val conn = Bootstrap.virtualMachineManager().listeningConnectors()
                .first { it.name() == "com.sun.jdi.SocketListen" }
            connector = conn
            val cargs = conn.defaultArguments()
            cargs["localAddress"]?.setValue("0.0.0.0")
            connectorArgs = cargs
            val address = conn.startListening(cargs)
            val port = address.substringAfterLast(":")
            val callback = callbackHost()
            console("pell debugger: listening on $callback:$port")

            val cmd = GeneralCommandLine(exe.absolutePath)
                .withParameters("debug-target", script, "--jdwp", "$callback:$port", "--wait-for-go")
                .withWorkDirectory(workDir)
                .withCharset(Charsets.UTF_8)
            val handler = OSProcessHandler(cmd)
            processHandler = handler
            session.consoleView?.attachToProcess(handler)
            handler.addProcessListener(object : com.intellij.execution.process.ProcessAdapter() {
                override fun processTerminated(event: com.intellij.execution.process.ProcessEvent) {
                    if (!stopped) ApplicationManager.getApplication().invokeLater { session.stop() }
                }
            })
            handler.startNotify()

            // 4. accept the DB's connection.
            val acceptedVm = conn.accept(cargs)
            vm = acceptedVm
            console("pell debugger: attached — ${acceptedVm.name()} ${acceptedVm.version()}")
            acceptedVm.eventRequestManager().createClassPrepareRequest().apply {
                setSuspendPolicy(EventRequest.SUSPEND_EVENT_THREAD)
                enable()
            }
            vmAttached.countDown()
            armWhatWeCan()
            acceptedVm.resume()

            // 5. release the target.
            sendGo()

            // 6. events.
            eventLoop(acceptedVm)
        } catch (e: Exception) {
            if (!stopped) {
                console("pell debugger failed: ${e.message}", ConsoleViewContentType.ERROR_OUTPUT)
                ApplicationManager.getApplication().invokeLater { session.stop() }
            }
        }
    }

    private fun sendGo() {
        if (goSent.compareAndSet(false, true)) {
            processHandler?.processInput?.let {
                it.write("go\n".toByteArray())
                it.flush()
            }
        }
    }

    /** Pick the address the database can reach US at: explicit env
     *  override, else host.docker.internal for a local container, else
     *  the local interface used to reach the DB host. */
    private fun callbackHost(): String {
        System.getenv("PELL_DEBUG_CALLBACK_HOST")?.takeIf { it.isNotBlank() }?.let { return it }
        val dbUrl = System.getenv("PELL_DB_URL") ?: ""
        val dbHost = dbUrl.substringAfter("@", "").substringBefore(":").substringBefore("/")
        if (dbHost.isBlank() || dbHost == "localhost" || dbHost.startsWith("127.")) {
            return "host.docker.internal"
        }
        return try {
            DatagramSocket().use { s ->
                s.connect(InetAddress.getByName(dbHost), 1521)
                s.localAddress.hostAddress
            }
        } catch (e: Exception) {
            InetAddress.getLocalHost().hostAddress
        }
    }

    // ------------------------------------------------------------------
    // breakpoints
    // ------------------------------------------------------------------

    private fun addBreakpoint(bp: XLineBreakpoint<*>) {
        val pos = bp.sourcePosition ?: return
        val filePath = pos.file.canonicalPath ?: return
        val pellLine = pos.line + 1
        val map = mapForFile(filePath) ?: run {
            console("pell debugger: no line map for ${pos.file.name} — breakpoint inactive",
                    ConsoleViewContentType.ERROR_OUTPUT)
            return
        }
        val target = map.resolveBreakpoint(pellLine) ?: run {
            console("pell debugger: no executable statement at/after ${pos.file.name}:$pellLine",
                    ConsoleViewContentType.ERROR_OUTPUT)
            return
        }
        synchronized(pending) { pending.add(Pending(target.first, target.second, bp)) }
        if (vmAttached.count == 0L) armWhatWeCan()
    }

    /** The srcmap that owns a file: the script's anon map, a cached
     *  module map, or a freshly computed one. */
    private fun mapForFile(filePath: String): PellSrcMap? {
        val canonical = File(filePath).canonicalPath
        if (canonical == config.filePath?.let { File(it).canonicalPath }) return scriptMap
        moduleMaps[canonical]?.let { return it }
        val exe = pellExe ?: return null
        val r = runPell(exe, "srcmap", canonical)
        if (r.first != 0) return null
        return PellSrcMap.parse(r.second).also { moduleMaps[canonical] = it }
    }

    /** Arm every pending breakpoint whose unit class is already loaded;
     *  the rest stay pending for ClassPrepare. */
    private fun armWhatWeCan() {
        val machine = vm ?: return
        val classes = try { machine.allClasses() } catch (e: Exception) { return }
        synchronized(pending) {
            val it = pending.iterator()
            while (it.hasNext()) {
                val p = it.next()
                val rt = classes.firstOrNull { c -> p.map.matches(c.name()) } ?: continue
                if (armOn(rt, p)) it.remove()
            }
        }
    }

    private fun armOn(rt: com.sun.jdi.ReferenceType, p: Pending): Boolean {
        return try {
            val locs = rt.locationsOfLine(p.unitLine)
            if (locs.isEmpty()) {
                console("pell debugger: ${rt.name()} has no code at unit line ${p.unitLine}",
                        ConsoleViewContentType.ERROR_OUTPUT)
                true  // drop it; never armable
            } else {
                val req = vm!!.eventRequestManager().createBreakpointRequest(locs[0])
                req.setSuspendPolicy(EventRequest.SUSPEND_ALL)
                req.enable()
                armed[req] = p.bp
                session.updateBreakpointPresentation(
                    p.bp,
                    com.intellij.icons.AllIcons.Debugger.Db_verified_breakpoint, null,
                )
                true
            }
        } catch (e: Exception) {
            false
        }
    }

    private fun removeBreakpoint(bp: XLineBreakpoint<*>) {
        synchronized(pending) { pending.removeAll { it.bp == bp } }
        val machine = vm ?: return
        armed.entries.filter { it.value == bp }.forEach { (req, _) ->
            runCatching { machine.eventRequestManager().deleteEventRequest(req) }
            armed.remove(req)
        }
    }

    // ------------------------------------------------------------------
    // event loop
    // ------------------------------------------------------------------

    private fun eventLoop(machine: VirtualMachine) {
        while (!stopped) {
            val events = try {
                machine.eventQueue().remove(500) ?: continue
            } catch (e: VMDisconnectedException) {
                break
            } catch (e: InterruptedException) {
                break
            }
            var suspendForUi = false
            for (ev in events) {
                when (ev) {
                    is ClassPrepareEvent -> armWhatWeCan()
                    is BreakpointEvent -> {
                        currentEventSet = events
                        currentThread = ev.thread()
                        val bp = armed[ev.request()]
                        val ctx = buildSuspendContext(ev.thread())
                        suspendForUi = true
                        ApplicationManager.getApplication().invokeLater {
                            if (bp != null) session.breakpointReached(bp, null, ctx)
                            else session.positionReached(ctx)
                        }
                    }
                    is StepEvent -> {
                        currentEventSet = events
                        currentThread = ev.thread()
                        runCatching { machine.eventRequestManager().deleteEventRequest(ev.request()) }
                        val ctx = buildSuspendContext(ev.thread())
                        suspendForUi = true
                        ApplicationManager.getApplication().invokeLater { session.positionReached(ctx) }
                    }
                    is VMDeathEvent, is VMDisconnectEvent -> stopped = true
                }
            }
            if (!suspendForUi && !stopped) {
                runCatching { events.resume() }
            }
        }
        if (!stopped) {
            stopped = true
            ApplicationManager.getApplication().invokeLater { session.stop() }
        }
    }

    private fun clearSteps(machine: VirtualMachine) {
        val erm = machine.eventRequestManager()
        erm.stepRequests().toList().forEach { runCatching { erm.deleteEventRequest(it) } }
    }

    private fun resumeTarget() {
        val es = currentEventSet
        currentEventSet = null
        runCatching { es?.resume() } .onFailure { runCatching { vm?.resume() } }
    }

    private fun step(depth: Int) {
        val machine = vm ?: return
        val thread = currentThread ?: return
        ApplicationManager.getApplication().executeOnPooledThread {
            runCatching {
                clearSteps(machine)
                machine.eventRequestManager().createStepRequest(
                    thread, StepRequest.STEP_LINE, depth,
                ).apply {
                    addCountFilter(1)
                    setSuspendPolicy(EventRequest.SUSPEND_ALL)
                    enable()
                }
                resumeTarget()
            }
        }
    }

    override fun startStepOver(context: XSuspendContext?) = step(StepRequest.STEP_OVER)
    override fun startStepInto(context: XSuspendContext?) = step(StepRequest.STEP_INTO)
    override fun startStepOut(context: XSuspendContext?) = step(StepRequest.STEP_OUT)

    override fun resume(context: XSuspendContext?) {
        ApplicationManager.getApplication().executeOnPooledThread {
            vm?.let { clearSteps(it) }
            resumeTarget()
        }
    }

    override fun stop() {
        stopped = true
        ApplicationManager.getApplication().executeOnPooledThread {
            runCatching { vm?.dispose() }
            runCatching {
                val c = connector
                val a = connectorArgs
                if (c != null && a != null) c.stopListening(a)
            }
            processHandler?.let { if (!it.isProcessTerminated) it.destroyProcess() }
        }
    }

    // ------------------------------------------------------------------
    // suspend context / frames / values
    // ------------------------------------------------------------------

    private fun buildSuspendContext(thread: ThreadReference): XSuspendContext {
        val frames = try { thread.frames() } catch (e: Exception) { emptyList() }
        val xframes = frames.mapIndexed { i, f -> PellStackFrame(f, i) }
        return object : XSuspendContext() {
            private val stack = object : XExecutionStack("Oracle session") {
                override fun getTopFrame(): XStackFrame? = xframes.firstOrNull()
                override fun computeStackFrames(firstFrameIndex: Int, container: XStackFrameContainer?) {
                    container?.addStackFrames(xframes.drop(firstFrameIndex), true)
                }
            }
            override fun getActiveExecutionStack(): XExecutionStack = stack
        }
    }

    private inner class PellStackFrame(
        private val frame: StackFrame,
        private val index: Int,
    ) : XStackFrame() {
        private val location = frame.location()
        private val className = location.declaringType().name()
        private val line = location.lineNumber()
        private val methodName = runCatching { location.method().name() }.getOrDefault("?")

        override fun getSourcePosition(): XSourcePosition? {
            // pell source first: the script's anon block, then module maps.
            if (className.contains(".Block.")) {
                val map = scriptMap?.units?.firstOrNull()
                val pellLine = map?.pellLineForUnit(line)
                if (pellLine != null) {
                    val vf = LocalFileSystem.getInstance().findFileByPath(config.filePath ?: return null)
                        ?: return null
                    return XDebuggerUtil.getInstance().createPosition(vf, pellLine - 1)
                }
                return null
            }
            for ((path, map) in moduleMaps) {
                val unit = map.unitFor(className) ?: continue
                val pellLine = unit.pellLineForUnit(line) ?: continue
                val vf = LocalFileSystem.getInstance().findFileByPath(path) ?: continue
                return XDebuggerUtil.getInstance().createPosition(vf, pellLine - 1)
            }
            // No pell source — show the deployed PL/SQL (ALL_SOURCE).
            val vf = foreignSourceFile(className) ?: return null
            return XDebuggerUtil.getInstance().createPosition(vf, (line - 1).coerceAtLeast(0))
        }

        override fun customizePresentation(component: com.intellij.ui.ColoredTextContainer) {
            val shortName = className.removePrefix("\$Oracle.")
            component.append("$shortName.$methodName:$line",
                com.intellij.ui.SimpleTextAttributes.REGULAR_ATTRIBUTES)
            component.setIcon(com.intellij.icons.AllIcons.Debugger.Frame)
        }

        override fun computeChildren(node: XCompositeNode) {
            ApplicationManager.getApplication().executeOnPooledThread {
                val children = XValueChildrenList()
                runCatching {
                    for (v in frame.visibleVariables()) {
                        val value = runCatching { frame.getValue(v) }.getOrNull()
                        children.add(PellValue(v.name(), value))
                    }
                }
                node.addChildren(children, true)
            }
        }
    }

    /** Fetch (once) the deployed source of a foreign unit so stepping
     *  continues into plain PL/SQL when pell source runs out. */
    private fun foreignSourceFile(className: String): VirtualFile? {
        foreignSource[className]?.let { return it }
        val exe = pellExe ?: return null
        // $Oracle.PackageBody.OWNER.NAME -> OWNER.NAME + PACKAGE_BODY
        val parts = className.split(".")
        if (parts.size < 4) return null
        val kind = when (parts[1]) {
            "PackageBody" -> "PACKAGE_BODY"
            "Package" -> "PACKAGE"
            "TypeBody" -> "TYPE_BODY"
            "Type" -> "TYPE"
            "Procedure" -> "PROCEDURE"
            "Function" -> "FUNCTION"
            "Trigger" -> "TRIGGER"
            else -> return null
        }
        val unit = "${parts[2]}.${parts[3]}"
        val r = runPell(exe, "debug-source", unit, "--type", kind)
        if (r.first != 0) return null
        val vf = LightVirtualFile("${unit}_${kind.lowercase()}.sql", r.second)
        vf.isWritable = false
        foreignSource[className] = vf
        return vf
    }

    private class PellValue(name: String, private val value: com.sun.jdi.Value?) : XNamedValue(name) {
        override fun computePresentation(node: XValueNode, place: XValuePlace) {
            val (text, type) = render(value)
            node.setPresentation(com.intellij.icons.AllIcons.Debugger.Value, type, text, false)
        }

        private fun render(v: com.sun.jdi.Value?): Pair<String, String?> = when (v) {
            null -> "null" to null
            is StringReference -> "\"${v.value()}\"" to "text"
            is PrimitiveValue -> v.toString() to null
            is ObjectReference -> renderObject(v)
            else -> v.toString() to null
        }

        /** Oracle hands locals back as $Oracle.Builtin.* object mirrors.
         *  toString() on the mirror gives "instance of ..."; the real
         *  value usually surfaces via the object's own toString method. */
        private fun renderObject(o: ObjectReference): Pair<String, String?> {
            val typeName = o.referenceType().name().removePrefix("\$Oracle.Builtin.")
            val rendered = runCatching {
                val m = o.referenceType().methodsByName("toString").firstOrNull()
                    ?: return@runCatching null
                val thread = o.virtualMachine().allThreads().firstOrNull { it.isSuspended }
                    ?: return@runCatching null
                val res = o.invokeMethod(thread, m, emptyList(), ObjectReference.INVOKE_SINGLE_THREADED)
                (res as? StringReference)?.value() ?: res?.toString()
            }.getOrNull()
            return (rendered ?: "<$typeName>") to typeName
        }
    }

    // ------------------------------------------------------------------
    // helpers
    // ------------------------------------------------------------------

    private fun runPell(exe: File, vararg args: String): Pair<Int, String> {
        return try {
            val p = ProcessBuilder(listOf(exe.absolutePath) + args)
                .directory(workDir)
                .redirectErrorStream(true)
                .start()
            val out = p.inputStream.bufferedReader().readText()
            p.waitFor(180, TimeUnit.SECONDS)
            p.exitValue() to out
        } catch (e: Exception) {
            -1 to (e.message ?: "process failed")
        }
    }

    private fun resolvePell(): File? {
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
}
