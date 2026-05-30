package dev.pell.intellij.toolwindow

import com.intellij.execution.process.OSProcessHandler
import com.intellij.execution.process.ProcessAdapter
import com.intellij.execution.process.ProcessEvent
import com.intellij.execution.configurations.GeneralCommandLine
import com.intellij.icons.AllIcons
import com.intellij.openapi.actionSystem.ActionManager
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnAction
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.actionSystem.DefaultActionGroup
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.fileEditor.FileEditorManager
import com.intellij.openapi.project.Project
import com.intellij.openapi.util.Key
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.treeStructure.Tree
import com.intellij.util.ui.JBUI
import java.awt.BorderLayout
import java.awt.Component
import java.awt.Dimension
import java.io.File
import javax.swing.JPanel
import javax.swing.JTextArea
import javax.swing.JTree
import javax.swing.SwingUtilities
import javax.swing.tree.DefaultMutableTreeNode
import javax.swing.tree.DefaultTreeCellRenderer
import javax.swing.tree.DefaultTreeModel
import javax.swing.tree.TreeSelectionModel

/**
 * The pell tool window UI. Vertical split:
 *
 *   ┌─────────────────────────────────────────────┐
 *   │ [↻] [▶] [▶▶] [⇧] [⇧⇧]      pell           │  ← toolbar
 *   ├─────────────────────────────────────────────┤
 *   │ ▶ hello.pell           ✓ built              │
 *   │ ▶ hr.employees         ✗ failed             │  ← module tree
 *   │ ▶ billing.charges      ↑ deployed           │
 *   ├─────────────────────────────────────────────┤
 *   │ ── log of last action ───                   │
 *   │ pell deploy: build → built/                 │
 *   │   ✓ hello.pell → hello.sql                  │  ← log panel
 *   │   ...                                       │
 *   └─────────────────────────────────────────────┘
 */
class PellToolWindow(private val project: Project) {

    private val service = PellModuleService.getInstance(project)
    private val rootNode = DefaultMutableTreeNode("pell modules")
    private val treeModel = DefaultTreeModel(rootNode)
    private val tree = Tree(treeModel).apply {
        isRootVisible = false
        showsRootHandles = false
        selectionModel.selectionMode = TreeSelectionModel.SINGLE_TREE_SELECTION
        cellRenderer = PellModuleCellRenderer()
    }
    private val log = JTextArea().apply {
        isEditable = false
        font = JBUI.Fonts.create("Monospaced", 12)
        margin = JBUI.insets(4)
    }

    val component: JPanel = JPanel(BorderLayout()).apply {
        val toolbar = buildToolbar()
        add(toolbar.component, BorderLayout.NORTH)

        val splitter = com.intellij.ui.OnePixelSplitter(true, 0.6f).apply {
            firstComponent = JBScrollPane(tree)
            secondComponent = JBScrollPane(log)
        }
        add(splitter, BorderLayout.CENTER)
    }

    init {
        // Refresh the module tree when the service signals a change.
        service.addListener { SwingUtilities.invokeLater { rebuildTree() } }
        // Double-click a row → open the source file.
        tree.addMouseListener(object : java.awt.event.MouseAdapter() {
            override fun mouseClicked(e: java.awt.event.MouseEvent) {
                if (e.clickCount != 2) return
                val mod = selectedModule() ?: return
                FileEditorManager.getInstance(project).openFile(mod.source, true)
            }
        })
        // Kick off the initial scan.
        service.rescan()
    }

    // -- toolbar -----------------------------------------------------

    private fun buildToolbar(): com.intellij.openapi.actionSystem.ActionToolbar {
        val group = DefaultActionGroup().apply {
            add(simpleAction("Refresh", AllIcons.Actions.Refresh) { service.rescan() })
            addSeparator()
            add(simpleAction("Build selected", AllIcons.Actions.Compile) {
                selectedModule()?.let { mod -> buildSelected(mod) }
            })
            add(simpleAction("Build all", AllIcons.Actions.RerunAutomatically) {
                runPell(listOf("deploy", project.basePath ?: ".", "--build-only", "--keep-going"), null)
            })
            addSeparator()
            add(simpleAction("Run SQL on PELL_DB_URL", AllIcons.RunConfigurations.TestState.Run) {
                selectedModule()?.let { mod -> runBuiltSql(mod) }
            })
            add(simpleAction("Deploy all", AllIcons.Actions.Upload) {
                runPell(listOf("deploy", project.basePath ?: ".", "--keep-going"), null)
            })
            addSeparator()
            add(simpleAction("Build pell_runtime.sql", AllIcons.Actions.GeneratedFolder) {
                runPell(
                    listOf(
                        "runtime",
                        project.basePath ?: ".",
                        "-o",
                        "${project.basePath}/${targetDir()}/pell_runtime.sql",
                    ),
                    null,
                )
            })
            add(simpleAction("Install pell_runtime on PELL_DB_URL", AllIcons.Toolwindows.ToolWindowChanges) {
                val out = File(project.basePath, "${targetDir()}/pell_runtime.sql")
                out.parentFile?.mkdirs()
                // Two-step: build the runtime SQL, then install it. The
                // chained `onSuccess` fires only when the first command
                // exits 0, so a generation failure won't try to install.
                runPell(
                    listOf("runtime", project.basePath ?: ".", "-o", out.absolutePath),
                    null,
                    onSuccess = {
                        SwingUtilities.invokeLater {
                            runPell(listOf("sql", out.absolutePath), null)
                        }
                    },
                )
            })
            addSeparator()
            add(simpleAction("Open pell REPL", AllIcons.Debugger.Console) {
                openRepl()
            })
            addSeparator()
            add(simpleAction("Open source", AllIcons.Actions.EditSource) {
                selectedModule()?.let { FileEditorManager.getInstance(project).openFile(it.source, true) }
            })
            add(simpleAction("Open built SQL", AllIcons.FileTypes.Custom) {
                selectedModule()?.built?.let { FileEditorManager.getInstance(project).openFile(it, true) }
            })
        }
        return ActionManager.getInstance().createActionToolbar("pell.toolbar", group, true).apply {
            targetComponent = component
        }
    }

    private fun simpleAction(label: String, icon: javax.swing.Icon, run: () -> Unit): AnAction =
        object : AnAction(label, label, icon) {
            override fun getActionUpdateThread() = ActionUpdateThread.BGT
            override fun actionPerformed(e: AnActionEvent) = run()
        }

    // -- tree --------------------------------------------------------

    private fun rebuildTree() {
        rootNode.removeAllChildren()
        for (mod in service.modules()) {
            rootNode.add(DefaultMutableTreeNode(mod))
        }
        treeModel.reload()
        // Expand the root so all rows are visible by default.
        for (i in 0 until tree.rowCount) tree.expandRow(i)
    }

    private fun selectedModule(): PellModule? {
        val node = tree.lastSelectedPathComponent as? DefaultMutableTreeNode ?: return null
        return node.userObject as? PellModule
    }

    // -- module actions ----------------------------------------------

    /** The target directory configured in Settings → Tools → pell.
     *  Falls back to `plsql` when no project or settings are available. */
    private fun targetDir(): String =
        dev.pell.intellij.settings.PellSettings.getInstance(project).targetDirName

    /** Compile a single .pell to `<project>/<targetDir>/<name>.sql`. Sets
     *  per-module state to Building, then BuildOk / BuildFailed on
     *  process completion (the runPell teardown handles that). */
    private fun buildSelected(mod: PellModule) {
        val out = File(project.basePath, targetDir()).also { it.mkdirs() }
        val outFile = File(out, mod.source.nameWithoutExtension + ".sql")
        runPell(
            listOf("build", mod.source.path, "-o", outFile.absolutePath),
            mod.source,
        )
    }

    /** Run the *built* .sql for the selected module via `pell sql`.
     *  If no built file exists OR the source is newer than the built
     *  copy, builds first, then runs. */
    private fun runBuiltSql(mod: PellModule) {
        val outDir = File(project.basePath, targetDir()).also { it.mkdirs() }
        val outFile = File(outDir, mod.source.nameWithoutExtension + ".sql")

        val needsBuild = !outFile.exists()
            || service.isStale(mod)
            || mod.built == null

        if (needsBuild) {
            log.append("# rebuilding ${mod.source.name} before run ...\n")
            // Synchronous-ish: queue build first, then run sql when it
            // finishes. We chain via the process listener — same pattern
            // the standard runPell uses.
            runPell(
                listOf("build", mod.source.path, "-o", outFile.absolutePath),
                mod.source,
                onSuccess = {
                    SwingUtilities.invokeLater {
                        service.rescan()
                        runPell(listOf("sql", outFile.absolutePath), mod.source)
                    }
                },
            )
        } else {
            runPell(listOf("sql", outFile.absolutePath), mod.source)
        }
    }

    /** Opens IntelliJ's terminal tool window and starts `./pell repl`.
     *  No file context — the REPL starts empty. (The gutter-icon path
     *  uses the same launcher with a `\load` for the current file.) */
    private fun openRepl() {
        if (dev.pell.intellij.run.PellReplLauncher.launch(project)) {
            log.append("\nopened pell repl in a terminal tab\n")
        } else {
            log.append("\nfailed to open terminal — run `./pell repl` manually.\n")
        }
    }

    // -- process launcher --------------------------------------------

    /** Shell out to the `pell` script and stream output into the log
     *  panel. Updates per-source state on completion. `onSuccess` is
     *  invoked on the EDT after a zero-exit process — used to chain
     *  build → sql in `runBuiltSql`. */
    private fun runPell(
        args: List<String>,
        sourceFile: VirtualFile?,
        onSuccess: (() -> Unit)? = null,
    ) {
        val pellExe = File(project.basePath, "pell")
        if (!pellExe.canExecute()) {
            log.append("error: ${pellExe.absolutePath} is not executable\n")
            return
        }
        // Append rather than replace — we want chained build→sql output
        // visible together. A single "Refresh"/"Build all" still wipes
        // when the user explicitly clicks them; for that pass null
        // sourceFile and call log.text = "" yourself.
        log.append("\n$ ${pellExe.absolutePath} ${args.joinToString(" ")}\n")
        sourceFile?.let { service.setState(it, PellModule.State.Building) }

        val cmd = GeneralCommandLine(pellExe.absolutePath)
            .withParameters(args)
            .withWorkDirectory(project.basePath)
            .withCharset(Charsets.UTF_8)

        try {
            val handler = OSProcessHandler(cmd)
            handler.addProcessListener(object : ProcessAdapter() {
                override fun onTextAvailable(event: ProcessEvent, outputType: Key<*>) {
                    val text = event.text
                    ApplicationManager.getApplication().invokeLater {
                        log.append(text)
                        log.caretPosition = log.document.length
                    }
                }
                override fun processTerminated(event: ProcessEvent) {
                    val ok = event.exitCode == 0
                    ApplicationManager.getApplication().invokeLater {
                        log.append("\n→ exit ${event.exitCode}\n")
                        if (sourceFile != null) {
                            // For `pell sql` runs, exit-0 means "deployed
                            // against the live DB"; for everything else
                            // it means "built ok". Detect by inspecting
                            // the args.
                            val isSqlRun = args.firstOrNull() == "sql"
                            service.setState(
                                sourceFile,
                                if (ok) {
                                    if (isSqlRun) PellModule.State.Deployed
                                    else PellModule.State.BuildOk
                                } else {
                                    if (isSqlRun) PellModule.State.DeployFailed
                                    else PellModule.State.BuildFailed
                                },
                            )
                            service.rescan()  // pick up newly-built .sql
                        } else {
                            // Bulk action: refresh state by reading the
                            // log output and matching ✓/✗ markers.
                            refreshStatesFromLog()
                            service.rescan()
                        }
                        if (ok) onSuccess?.invoke()
                    }
                }
            })
            handler.startNotify()
        } catch (e: Exception) {
            log.append("error: ${e.message}\n")
        }
    }

    /** Parse `✓ foo.pell → foo.sql` and `✗ foo.pell: ...` lines out of
     *  the log to bulk-update module state after a deploy-all run. */
    private fun refreshStatesFromLog() {
        val byName = service.modules().associateBy { it.source.name }
        val text = log.text
        val okRegex = Regex("""(?m)^\s*[✓]\s+(\S+\.pell)\s*→""")
        val errRegex = Regex("""(?m)^\s*[✗]\s+(\S+\.pell):""")
        okRegex.findAll(text).forEach {
            byName[it.groupValues[1]]?.let { mod ->
                service.setState(mod.source, PellModule.State.BuildOk)
            }
        }
        errRegex.findAll(text).forEach {
            byName[it.groupValues[1]]?.let { mod ->
                service.setState(mod.source, PellModule.State.BuildFailed, it.value)
            }
        }
        // Deploy lines look the same shape; if the build succeeded and
        // the deploy half ran too, treat ✓ entries as Deployed.
        if (text.contains("pell deploy: install ")) {
            okRegex.findAll(text.substringAfter("pell deploy: install ")).forEach {
                byName[it.groupValues[1]]?.let { mod ->
                    service.setState(mod.source, PellModule.State.Deployed)
                }
            }
        }
    }
}

/**
 * Renderer for module rows — name on the left, state icon + label on
 * the right. Uses IntelliJ's built-in icons so the panel feels native.
 */
private class PellModuleCellRenderer : DefaultTreeCellRenderer() {
    override fun getTreeCellRendererComponent(
        tree: JTree?, value: Any?, sel: Boolean, expanded: Boolean,
        leaf: Boolean, row: Int, hasFocus: Boolean,
    ): Component {
        val c = super.getTreeCellRendererComponent(tree, value, sel, expanded, leaf, row, hasFocus)
        if (c !is JBLabel && c !is javax.swing.JLabel) return c
        val node = value as? DefaultMutableTreeNode ?: return c
        val mod = node.userObject as? PellModule ?: run {
            (c as javax.swing.JLabel).text = node.userObject?.toString() ?: ""
            (c as javax.swing.JLabel).icon = AllIcons.Nodes.Folder
            return c
        }
        val label = c as javax.swing.JLabel
        val builtSuffix = mod.built?.let { built ->
            val stale = mod.source.timeStamp > built.timeStamp
            if (stale) " → ${built.name} (stale)" else " → ${built.name}"
        } ?: ""
        val state = when (mod.state) {
            PellModule.State.Unknown      -> "(no .sql)"
            PellModule.State.Building     -> "building…"
            PellModule.State.BuildOk      -> "built$builtSuffix"
            PellModule.State.BuildFailed  -> "build failed"
            PellModule.State.Deploying    -> "deploying…"
            PellModule.State.Deployed     -> "deployed$builtSuffix"
            PellModule.State.DeployFailed -> "deploy failed"
        }
        label.text = "${mod.source.name}    ${mod.moduleName}   — $state"
        label.icon = when (mod.state) {
            PellModule.State.Unknown      -> AllIcons.Nodes.Pluginobsolete
            PellModule.State.Building, PellModule.State.Deploying -> AllIcons.Process.Step_1
            PellModule.State.BuildOk      -> AllIcons.Actions.Commit
            PellModule.State.BuildFailed  -> AllIcons.General.Error
            PellModule.State.Deployed     -> AllIcons.Actions.Upload
            PellModule.State.DeployFailed -> AllIcons.General.Warning
        }
        return c
    }
}
