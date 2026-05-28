package dev.pell.intellij.newproject

import java.io.File

/**
 * Writes the initial file tree for a new pell project. No IntelliJ
 * dependencies in here on purpose — keeps the scaffold easy to test
 * and easy to reuse from CLI later.
 *
 * Produces:
 *
 *   <root>/
 *   ├── README.md               — project intro
 *   ├── .gitignore              — built/, venv, etc.
 *   ├── pell                    — wrapper script that finds the pell CLI
 *   ├── src/
 *   │   └── hello.pell          — starter module
 *   ├── tests/                  — empty, ready for test_*.pell
 *   ├── built/                  — output dir (gitignored)
 *   └── .idea/runConfigurations/
 *       └── pell_deploy_all.xml — one-click "Deploy all" config
 */
object PellProjectScaffold {

    /** Creates the directory tree under [root], filled with starter
     *  files. Existing files are left alone — this is idempotent. */
    fun create(root: File, projectName: String, pellHome: String?) {
        root.mkdirs()
        File(root, "src").mkdirs()
        File(root, "tests").mkdirs()
        File(root, "built").mkdirs()
        File(root, ".idea/runConfigurations").mkdirs()

        writeIfMissing(File(root, "README.md"), readme(projectName))
        writeIfMissing(File(root, ".gitignore"), gitignore())
        writeIfMissing(File(root, "src/hello.pell"), helloPell(projectName))
        writeIfMissing(File(root, "pell"), pellShim(pellHome).also { _ ->
            // mark executable below
        })
        writeIfMissing(
            File(root, ".idea/runConfigurations/pell_deploy_all.xml"),
            deployAllRunConfig(root),
        )

        // chmod +x pell shim
        File(root, "pell").setExecutable(true, false)
    }

    private fun writeIfMissing(file: File, content: String) {
        if (file.exists()) return
        file.parentFile?.mkdirs()
        file.writeText(content)
    }

    private fun readme(name: String): String = """
        # $name

        A [pell](https://github.com/batterts/pell) project — statically-typed
        source that compiles to Oracle PL/SQL.

        ## Quick start

        ```sh
        # Compile everything to ./built/
        ./pell deploy . --build-only

        # Compile + install against PELL_DB_URL
        export PELL_DB_URL='user/pass@host:1521/service'
        ./pell deploy .
        ```

        ## Layout

        - `src/` — `.pell` source files (one per module)
        - `tests/` — test files (`test_*.pell`)
        - `built/` — compiler output, gitignored
        - `pell` — wrapper script that finds the pell CLI

        ## Resources

        - [pell docs](https://github.com/batterts/pell/tree/main/docs)
        - [pell tutorial](https://github.com/batterts/pell/tree/main/docs/tutorial)
        - [pell reference](https://github.com/batterts/pell/tree/main/docs/reference)
    """.trimIndent() + "\n"

    private fun gitignore(): String = """
        # Compiled artifacts
        built/
        *.sql.tmp

        # Local environment
        .env
        .venv/
        .idea/workspace.xml
        .idea/tasks.xml
        .DS_Store

        # Pell working dirs
        .understand-anything/
    """.trimIndent() + "\n"

    private fun helloPell(name: String): String = """
        module hello;

        // Your first pell module. Compile with:
        //     ./pell build src/hello.pell
        //
        // Or run via the IntelliJ gutter ▶ button.

        pub fn greet(who: text) -> text {
            return "hello, {who} from $name";
        }
    """.trimIndent() + "\n"

    /** Wrapper script. Resolves `pell` from (in order):
     *    1. PELL_HOME env var (must point at the pell repo root)
     *    2. The path the user gave at project-creation time
     *    3. `pell` on PATH
     *    4. Fall back to a helpful error.
     */
    private fun pellShim(pellHome: String?): String {
        val hint = pellHome?.takeIf { it.isNotBlank() }?.let {
            "DEFAULT_PELL_HOME=\"$it\""
        } ?: "DEFAULT_PELL_HOME=\"\""
        return """
            #!/bin/sh
            # pell — wrapper that locates the pell CLI for this project.
            # Resolves in order: ${'$'}PELL_HOME, baked-in default, ${'$'}PATH.

            $hint

            if [ -n "${'$'}{PELL_HOME:-}" ] && [ -x "${'$'}PELL_HOME/pell" ]; then
              exec "${'$'}PELL_HOME/pell" "${'$'}@"
            fi
            if [ -n "${'$'}DEFAULT_PELL_HOME" ] && [ -x "${'$'}DEFAULT_PELL_HOME/pell" ]; then
              exec "${'$'}DEFAULT_PELL_HOME/pell" "${'$'}@"
            fi
            if command -v pell >/dev/null 2>&1; then
              exec pell "${'$'}@"
            fi

            echo "pell: couldn't find the pell CLI." >&2
            echo "  Set PELL_HOME to your pell repo, or put pell on PATH." >&2
            exit 127
        """.trimIndent() + "\n"
    }

    private fun deployAllRunConfig(root: File): String = """
        <component name="ProjectRunConfigurationManager">
          <configuration default="false" name="pell deploy (all)" type="ShConfigurationType">
            <option name="SCRIPT_TEXT" value="./pell deploy . --keep-going" />
            <option name="INDEPENDENT_SCRIPT_PATH" value="true" />
            <option name="SCRIPT_PATH" value="" />
            <option name="SCRIPT_OPTIONS" value="" />
            <option name="INDEPENDENT_SCRIPT_WORKING_DIRECTORY" value="true" />
            <option name="SCRIPT_WORKING_DIRECTORY" value="${'$'}PROJECT_DIR${'$'}" />
            <option name="INDEPENDENT_INTERPRETER_PATH" value="true" />
            <option name="INTERPRETER_PATH" value="/bin/sh" />
            <option name="INTERPRETER_OPTIONS" value="" />
            <option name="EXECUTE_IN_TERMINAL" value="false" />
            <option name="EXECUTE_SCRIPT_FILE" value="false" />
            <envs />
            <method v="2" />
          </configuration>
        </component>
    """.trimIndent() + "\n"
}
