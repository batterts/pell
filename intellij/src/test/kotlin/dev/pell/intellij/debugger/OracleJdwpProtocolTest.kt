package dev.pell.intellij.debugger

import com.sun.jdi.Bootstrap
import com.sun.jdi.VirtualMachine
import com.sun.jdi.event.BreakpointEvent
import com.sun.jdi.event.ClassPrepareEvent
import com.sun.jdi.event.StepEvent
import com.sun.jdi.request.EventRequest
import com.sun.jdi.request.StepRequest
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue

/**
 * Protocol-level proof that the pell debugger pipeline works end to end
 * against a real Oracle:
 *
 *   pell deploy --debug   (markers + PLSQL_DEBUG units)
 *   pell srcmap           (pell line -> unit line)
 *   pell debug-target     (DBMS_DEBUG_JDWP.CONNECT_TCP back to us)
 *   JDI ListeningConnector: ClassPrepare on $Oracle.* -> breakpoint at
 *   the mapped line -> hit -> step -> resume -> block completes.
 *
 * Needs a database + connect-back path, so it's skipped unless
 * PELL_DEBUG_TEST_DB is set, e.g. (docker compose oracle on :11521):
 *
 *   PELL_DEBUG_TEST_DB=pell_test/pell_test@localhost:11521/FREEPDB1 \
 *   PELL_DEBUG_CALLBACK_HOST=host.docker.internal \
 *   ./gradlew test --tests '*OracleJdwpProtocolTest*'
 *
 * This is the contract the IntelliJ debug process (PellDebugProcess)
 * is built on — if Oracle changes JDWP behavior, this fails first.
 */
class OracleJdwpProtocolTest {

    private val dbUrl: String? = System.getenv("PELL_DEBUG_TEST_DB")
    private val callbackHost: String =
        System.getenv("PELL_DEBUG_CALLBACK_HOST") ?: "host.docker.internal"

    private val repo: File by lazy {
        // gradle runs tests with cwd = intellij/
        File(System.getProperty("user.dir")).parentFile
    }
    private val examples: File get() = File(repo, "compiler/examples")

    private fun pell(vararg args: String, cwd: File = repo): Pair<Int, String> {
        val pb = ProcessBuilder(listOf(File(repo, "pell").absolutePath) + args)
            .directory(cwd)
            .redirectErrorStream(true)
        pb.environment()["PELL_DB_URL"] = dbUrl
        val p = pb.start()
        val out = p.inputStream.bufferedReader().readText()
        p.waitFor(120, TimeUnit.SECONDS)
        return p.exitValue() to out
    }

    @Test
    fun breakpointHitStepAndResume() {
        assumeTrue("PELL_DEBUG_TEST_DB not set — JDWP protocol test skipped", dbUrl != null)

        // 1. Debug-deploy the example.
        val (rc, out) = pell("deploy", "compiler/examples/01_hello.pell", "--debug",
                             "--out-dir", createTempDir("pelldbg").absolutePath)
        assertEquals("deploy --debug failed:\n$out", 0, rc)

        // 2. Line map: pell line -> unit line of the deployed body.
        val (rcMap, mapOut) = pell("srcmap", "compiler/examples/01_hello.pell")
        assertEquals("srcmap failed:\n$mapOut", 0, rcMap)
        // Entries look like {"pell_line": 10, "unit_line": 5} inside the
        // PACKAGE BODY unit. Cheap extraction — no JSON dep needed here.
        val bodyChunk = mapOut.substringAfter("PackageBody").substringBefore("]")
        val entries = Regex(""""pell_line":\s*(\d+),\s*"unit_line":\s*(\d+)""")
            .findAll(bodyChunk)
            .associate { it.groupValues[1].toInt() to it.groupValues[2].toInt() }
        val bpUnitLine = entries[10] ?: error("no map entry for pell line 10 in:\n$mapOut")

        // 3. Listen for the database's JDWP connection.
        val connector = Bootstrap.virtualMachineManager().listeningConnectors()
            .first { it.name() == "com.sun.jdi.SocketListen" }
        val connArgs = connector.defaultArguments()
        connArgs["localAddress"]?.setValue("0.0.0.0")
        // Pin the listener port for tunnel setups (reverse ssh forwards
        // need a fixed port); blank = ephemeral.
        System.getenv("PELL_DEBUG_TEST_PORT")?.takeIf { it.isNotBlank() }?.let {
            connArgs["port"]?.setValue(it)
        }
        val address = connector.startListening(connArgs)
        val port = address.substringAfterLast(":")

        // 4. Launch the target session.
        val stub = File.createTempFile("debug_stub", ".pell").apply {
            // The loop keeps the block alive long enough to observe class
            // loading and hit a mid-run breakpoint on a later iteration.
            writeText(
                """
                import hello;
                for i in 1..=20 {
                    let x = hello::greet("world");
                }
                p(hello::greet("world"));
                """.trimIndent() + "\n",
            )
        }
        val targetPb = ProcessBuilder(
            File(repo, "pell").absolutePath, "debug-target",
            stub.absolutePath, "--jdwp", "$callbackHost:$port", "--wait-for-go",
        ).directory(examples).redirectErrorStream(true)
        targetPb.environment()["PELL_DB_URL"] = dbUrl
        val target = targetPb.start()
        val targetOut = StringBuilder()
        val reader = Thread {
            target.inputStream.bufferedReader().forEachLine { targetOut.appendLine(it) }
        }.apply { start() }

        var vm: VirtualMachine? = null
        try {
            vm = connector.accept(connArgs)
            println("JDWP attached: ${vm.name()} / ${vm.version()}")

            println("classes at attach: ${vm.allClasses().size}")
            vm.allClasses().take(30).forEach { println("  pre-loaded: ${it.name()}") }

            // No filter — log every prepare to learn Oracle's naming.
            val prep = vm.eventRequestManager().createClassPrepareRequest().apply {
                setSuspendPolicy(EventRequest.SUSPEND_EVENT_THREAD)
                enable()
            }
            var bpSet = false
            fun tryArmBreakpoint(): Boolean {
                val classes = vm.allClasses().filter {
                    it.name().contains("HELLO") && it.name().contains("PackageBody")
                }
                for (rt in classes) {
                    println("found class: ${rt.name()} strata=${rt.availableStrata()}")
                    println("  lines=" + runCatching {
                        rt.allLineLocations().joinToString { it.lineNumber().toString() }
                    }.getOrElse { "unavailable: $it" })
                    val locs = runCatching { rt.locationsOfLine(bpUnitLine) }.getOrDefault(emptyList())
                    if (locs.isNotEmpty()) {
                        vm.eventRequestManager().createBreakpointRequest(locs[0]).apply {
                            setSuspendPolicy(EventRequest.SUSPEND_EVENT_THREAD)
                            enable()
                        }
                        println("breakpoint armed at unit line $bpUnitLine")
                        return true
                    }
                }
                return false
            }

            var hitLine = -1
            var hitType = ""
            var sawGreetFrame = false
            var sawBlockFrameWithLine = false
            var sawLocals = false
            var steppedLine = -1
            var done = false
            val deadline = System.currentTimeMillis() + 60_000
            vm.resume()

            // Requests armed — release the target (it waits on stdin so the
            // block can't outrun our breakpoint setup).
            target.outputStream.write("go\n".toByteArray())
            target.outputStream.flush()

            while (!done && System.currentTimeMillis() < deadline) {
                if (!bpSet) bpSet = tryArmBreakpoint()
                val events = try {
                    vm.eventQueue().remove(1_000) ?: continue
                } catch (e: com.sun.jdi.VMDisconnectedException) {
                    println("VM disconnected (block finished)")
                    break
                }
                for (ev in events) {
                    println("event: $ev")
                    when (ev) {
                        is ClassPrepareEvent -> {
                            val rt = ev.referenceType()
                            println("prepared: ${rt.name()}")
                            if (!bpSet) bpSet = tryArmBreakpoint()
                        }
                        is BreakpointEvent -> {
                            hitLine = ev.location().lineNumber()
                            hitType = ev.location().declaringType().name()
                            println("breakpoint hit at line $hitLine in $hitType")
                            val frames = ev.thread().frames()
                            println("frames:")
                            frames.forEach {
                                println("  ${it.location().declaringType().name()}:" +
                                        "${it.location().lineNumber()} " +
                                        "method=${it.location().method().name()}")
                                if (it.location().method().name() == "GREET") sawGreetFrame = true
                                if (it.location().declaringType().name().contains(".Block.") &&
                                    it.location().lineNumber() > 0
                                ) sawBlockFrameWithLine = true
                            }
                            runCatching {
                                val visible = frames[0].visibleVariables()
                                if (visible.isNotEmpty()) sawLocals = true
                                println("locals: " + visible.joinToString {
                                    "${it.name()}=${runCatching { frames[0].getValue(it) }.getOrNull()}"
                                })
                                // Pin the value-read contract: Oracle scalar
                                // mirrors have NO methods; the value lives in
                                // a `_value` field (what PellValue reads).
                                val mirror = visible.firstNotNullOfOrNull { vr ->
                                    runCatching { frames[0].getValue(vr) }.getOrNull()
                                        as? com.sun.jdi.ObjectReference
                                }
                                if (mirror != null) {
                                    val f = mirror.referenceType().allFields()
                                        .firstOrNull { it.name() == "_value" }
                                    assertTrue("scalar mirror must carry _value field " +
                                        "(fields=${mirror.referenceType().allFields().map { it.name() }})",
                                        f != null)
                                    println("mirror _value = ${mirror.getValue(f)}")
                                }
                            }.onFailure { println("locals unavailable: $it") }
                            vm.eventRequestManager().deleteEventRequest(ev.request())
                            vm.eventRequestManager().createStepRequest(
                                ev.thread(), StepRequest.STEP_LINE, StepRequest.STEP_OVER,
                            ).apply {
                                addCountFilter(1)
                                setSuspendPolicy(EventRequest.SUSPEND_EVENT_THREAD)
                                enable()
                            }
                        }
                        is StepEvent -> {
                            steppedLine = ev.location().lineNumber()
                            println("stepped to line $steppedLine in " +
                                    ev.location().declaringType().name())
                            vm.eventRequestManager().deleteEventRequest(ev.request())
                            done = true
                        }
                    }
                }
                events.resume()
            }

            assertEquals("breakpoint should hit the mapped unit line", bpUnitLine, hitLine)
            assertTrue("breakpoint should be in the HELLO package body (got $hitType)",
                       hitType == "\$Oracle.PackageBody.PELL_TEST.HELLO")
            assertTrue("stack should contain the GREET frame", sawGreetFrame)
            assertTrue("anon-block frame should carry a line number " +
                       "(debug-target must compile the block with PLSQL_DEBUG)",
                       sawBlockFrameWithLine)
            assertTrue("frame locals should be visible", sawLocals)
            // Step semantics: Oracle's STEP_OVER stops at the next line
            // boundary at the same-or-shallower frame DEPTH — when the
            // current fn returns and is re-entered (loops), the landing
            // line can be numerically smaller. Assert arrival, not order.
            assertTrue("a StepEvent should arrive after STEP_OVER", steppedLine >= 0)
        } finally {
            // Let the block run to completion so debug-target exits cleanly.
            runCatching {
                vm?.eventRequestManager()?.deleteAllBreakpoints()
                vm?.resume()
                vm?.dispose()
            }
            target.waitFor(120, TimeUnit.SECONDS)
            reader.join(5_000)
            println("debug-target output:\n$targetOut")
        }
        assertTrue("target should print the fn result:\n$targetOut",
                   targetOut.contains("hello, world"))
    }
}
