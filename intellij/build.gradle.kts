// pell-intellij — IntelliJ plugin that registers a `.pell` file type and
// adds a green-arrow gutter icon + run configurations for `pell build`,
// `pell exec`, `pell parse`, `pell tokens`.
//
// LSP wiring (syntax highlight / diagnostics / hover) is handled by
// LSP4IJ's user-defined-server settings — the `pell-intellij` skill
// configures that side. This plugin focuses on the file type +
// run-configuration ergonomics that need real IntelliJ-platform code.
//
// Build with:
//     ./gradlew buildPlugin
// Produces: build/distributions/pell-intellij-<version>.zip
// Install via Settings → Plugins → ⚙ → Install Plugin from Disk → pick the .zip

plugins {
    java
    id("org.jetbrains.kotlin.jvm") version "2.0.21"
    id("org.jetbrains.intellij.platform") version "2.2.1"
}

group = "dev.pell"
version = "0.3.4"

repositories {
    mavenCentral()
    intellijPlatform { defaultRepositories() }
}

dependencies {
    intellijPlatform {
        intellijIdeaCommunity("2024.3")
        // Terminal plugin — needed so we can open `./pell repl` inside
        // IntelliJ's terminal tool window (the REPL needs a real TTY,
        // which the Run console doesn't have). The Terminal plugin is
        // bundled with every IntelliJ-family IDE.
        bundledPlugin("org.jetbrains.plugins.terminal")
        instrumentationTools()
        pluginVerifier()
        zipSigner()
    }
}

intellijPlatform {
    pluginConfiguration {
        ideaVersion {
            sinceBuild = "243"      // IntelliJ 2024.3+
            untilBuild = provider { null }   // no upper bound — works on 2024.3, 2025.x, 2026.x, ...
        }
    }
    // Suppress the "until-build is missing" warning that fires when we
    // intentionally don't bound the upper range.
    pluginVerification {
        ides {
            // Pin to the IDE we develop against — `recommended()` requests
            // versions that may not be in the local cache. We can still
            // ship to newer IDEs (untilBuild is unbounded); the
            // Marketplace's own verifier runs against the full matrix.
            ide("IC", "2024.3")
        }
    }
}

kotlin {
    jvmToolchain(21)
}

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}
