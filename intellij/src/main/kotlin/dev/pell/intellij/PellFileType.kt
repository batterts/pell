package dev.pell.intellij

import com.intellij.openapi.fileTypes.LanguageFileType
import javax.swing.Icon

/** File type for `.pell` files. */
object PellFileType : LanguageFileType(PellLanguage) {
    @JvmField
    val INSTANCE: PellFileType = this

    override fun getName(): String = "pell"
    override fun getDescription(): String = "pell language source"
    override fun getDefaultExtension(): String = "pell"
    override fun getIcon(): Icon? = null  // TODO: ship a 16x16 SVG icon
}
