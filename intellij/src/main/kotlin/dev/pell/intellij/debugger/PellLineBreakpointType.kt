package dev.pell.intellij.debugger

import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.VirtualFile
import com.intellij.xdebugger.breakpoints.XBreakpointProperties
import com.intellij.xdebugger.breakpoints.XLineBreakpointType

/**
 * Gutter breakpoints for .pell files. Placement is permissive (any
 * line of a .pell file) — at debug time the srcmap snaps the
 * breakpoint to the nearest following statement marker, mirroring how
 * Oracle snaps PL/SQL breakpoints to executable lines.
 */
class PellLineBreakpointType :
    XLineBreakpointType<XBreakpointProperties<*>>(ID, "pell Line Breakpoint") {

    override fun canPutAt(file: VirtualFile, line: Int, project: Project): Boolean =
        file.extension == "pell"

    override fun createBreakpointProperties(file: VirtualFile, line: Int): XBreakpointProperties<*>? = null

    companion object {
        const val ID = "pell-line"
    }
}
