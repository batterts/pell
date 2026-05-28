package dev.pell.intellij.toolwindow

import com.intellij.icons.AllIcons
import com.intellij.openapi.project.Project
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.ui.content.ContentFactory

/**
 * Side-panel registration. Surfaces a "pell" tool window with an icon
 * in the strip alongside Maven / Database / Project / etc.
 *
 * The bulk of the UI lives in `PellToolWindow` so it can grow without
 * cluttering this factory.
 */
class PellToolWindowFactory : ToolWindowFactory {

    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        // Give the tab a recognizable icon — using the standard "compiled" icon as a
        // pragmatic placeholder until we ship a custom 13x13 SVG.
        toolWindow.setIcon(AllIcons.Actions.Compile)

        val ui = PellToolWindow(project)
        val content = ContentFactory.getInstance().createContent(ui.component, "", false)
        toolWindow.contentManager.addContent(content)
    }

    override fun shouldBeAvailable(project: Project): Boolean = true
}
