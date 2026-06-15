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
    id("org.jetbrains.kotlin.jvm") version "1.9.24"
    id("org.jetbrains.intellij.platform") version "2.2.1"
    // >>> PSI TRACK >>>
    // Grammar-Kit generates the parser from src/main/grammar/Pell.bnf;
    // JFlex generates the lexer from src/main/flex/Pell.flex. Outputs
    // land in src/main/gen/ (gitignored) and are added to the Java
    // source set below so they compile alongside the hand-written code.
    id("org.jetbrains.grammarkit") version "2022.3.2.2"
    // <<< PSI TRACK <<<
}

group = "dev.pell"
version = "0.9.12"

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
        // Platform test fixtures — enables CodeInsightTestFixture
        // (myFixture.findUsages, rename, etc.) for headless PSI tests.
        testFramework(org.jetbrains.intellij.platform.gradle.TestFrameworkType.Platform)
    }
    testImplementation("junit:junit:4.13.2")
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
    // Publishing to JetBrains Marketplace.
    //   ./gradlew publishPlugin
    // Requires:
    //   JETBRAINS_MARKETPLACE_TOKEN env var (get one from
    //   https://plugins.jetbrains.com/author/me/tokens).
    // The publishPlugin task signs the bundle, uploads via the
    // Marketplace REST API, and bumps the version row on plugins.jetbrains.com.
    publishing {
        token = providers.environmentVariable("JETBRAINS_MARKETPLACE_TOKEN")
        // Optional channel — leave empty for stable. Set to "eap" or
        // "beta" if you want pre-release distribution.
        channels = providers.environmentVariable("JETBRAINS_MARKETPLACE_CHANNEL")
            .map { listOf(it) }
            .orElse(listOf("default"))
    }
}

kotlin {
    jvmToolchain(21)
}

java {
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}

// >>> PSI TRACK >>>
// Generated parser / lexer go into src/main/gen and are picked up by
// both the Java and Kotlin compile tasks. The gen dir is gitignored.
sourceSets {
    main {
        java.srcDirs("src/main/gen")
    }
}

tasks.generateLexer {
    sourceFile.set(file("src/main/flex/Pell.flex"))
    targetOutputDir.set(file("src/main/gen/dev/pell/intellij/parser"))
    purgeOldFiles.set(true)
}
tasks.generateParser {
    sourceFile.set(file("src/main/grammar/Pell.bnf"))
    targetRootOutputDir.set(file("src/main/gen"))
    pathToParser.set("dev/pell/intellij/parser/PellParser.java")
    pathToPsiRoot.set("dev/pell/intellij/psi")
    purgeOldFiles.set(true)
}
tasks.named("compileKotlin") {
    dependsOn(tasks.generateLexer, tasks.generateParser)
}
tasks.named("compileJava") {
    dependsOn(tasks.generateLexer, tasks.generateParser)
}
tasks.named("clean") {
    doLast { delete("src/main/gen") }
}
// <<< PSI TRACK <<<
