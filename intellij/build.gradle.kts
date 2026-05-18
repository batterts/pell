// pell-intellij — IntelliJ plugin that registers a `.pell` file type and
// wires up the pell-lsp server (via LSP4IJ) so diagnostics/hover/completion/
// document-symbols all work in IntelliJ-family IDEs (IDEA, DataGrip, etc.).
//
// Build with:
//     ./gradlew buildPlugin
// Produces: build/distributions/pell-intellij-<version>.zip
// Install via Settings → Plugins → ⚙ → Install Plugin from Disk → pick the .zip

plugins {
    java
    id("org.jetbrains.kotlin.jvm") version "1.9.25"
    id("org.jetbrains.intellij.platform") version "2.2.1"
}

group = "dev.pell"
version = "0.0.1"

repositories {
    mavenCentral()
    intellijPlatform { defaultRepositories() }
}

dependencies {
    intellijPlatform {
        intellijIdeaCommunity("2024.3")
        // LSP4IJ — the open-source LSP-to-IntelliJ-platform bridge.
        // Available on the JetBrains Marketplace; we depend on it at runtime.
        plugin("com.redhat.devtools.lsp4ij:0.10.0")

        instrumentationTools()
        pluginVerifier()
        zipSigner()
    }
}

intellijPlatform {
    pluginConfiguration {
        ideaVersion {
            sinceBuild = "243"   // IntelliJ 2024.3+
        }
    }
}

kotlin {
    jvmToolchain(17)
}
