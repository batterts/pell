package dev.pell.intellij.settings

import com.intellij.notification.NotificationAction
import com.intellij.notification.NotificationGroupManager
import com.intellij.notification.NotificationType
import com.intellij.openapi.options.ShowSettingsUtil
import com.intellij.openapi.project.Project
import com.intellij.openapi.startup.ProjectActivity

/**
 * Fires once per project open. If the user's environment has no
 * working pell CLI, surface a friendly notification with two actions:
 *   - "Install pell" → opens Settings → Tools → pell where the
 *     one-click install button lives
 *   - "Get instructions" → opens the docs page
 *
 * Silent when pell IS detected (the common case after first run).
 *
 * Only nags once per project per session — backed by a project user
 * data flag so re-opens within the same IDE process don't repeat.
 */
class PellFirstLaunchActivity : ProjectActivity {

    override suspend fun execute(project: Project) {
        // Skip if we already nagged this project in this IDE session.
        if (project.getUserData(NAG_KEY) == true) return
        project.putUserData(NAG_KEY, true)

        // Cheap synchronous probe — just checks file existence.
        if (PellDetector.locate(project) != null) return

        NotificationGroupManager.getInstance()
            .getNotificationGroup("pell")
            ?.createNotification(
                "pell CLI not found",
                "The pell plugin needs the <code>pell</code> command-line tool. " +
                "Install with <code>pip install pell</code> (or open Settings → " +
                "Tools → pell for a one-click button).",
                NotificationType.INFORMATION,
            )
            ?.addAction(NotificationAction.createSimple(
                "Install pell",
                Runnable {
                    ShowSettingsUtil.getInstance()
                        .showSettingsDialog(project, "dev.pell.settings")
                },
            ))
            ?.addAction(NotificationAction.createSimple(
                "Get instructions",
                Runnable {
                    com.intellij.ide.BrowserUtil.browse("https://batterts.github.io/pell")
                },
            ))
            ?.notify(project)
    }

    companion object {
        private val NAG_KEY = com.intellij.openapi.util.Key.create<Boolean>("pell.first-launch-checked")
    }
}
