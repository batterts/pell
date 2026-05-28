package dev.pell.intellij

import com.intellij.openapi.fileTypes.LanguageFileType
import javax.swing.Icon

/** File type for `.pell` files. The Kotlin `object` declaration
 *  exposes itself as the `INSTANCE` field via JvmStatic, which
 *  plugin.xml references with `fieldName="INSTANCE"`. */
object PellFileType : LanguageFileType(PellLanguage) {
    override fun getName(): String = "pell"
    override fun getDescription(): String = "pell language source"
    override fun getDefaultExtension(): String = "pell"
    override fun getIcon(): Icon? = null  // TODO: ship a 16x16 SVG icon
}
