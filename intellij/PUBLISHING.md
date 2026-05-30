# Publishing pell to JetBrains Marketplace

## One-time setup

1. Generate a Marketplace API token at
   https://plugins.jetbrains.com/author/me/tokens (Permanent token,
   "Upload plugin" scope is enough).
2. Add to your shell profile (`.zshrc` / `.bashrc`):

   ```sh
   export JETBRAINS_MARKETPLACE_TOKEN='perm-...'
   ```

   The Gradle plugin reads this env var; it's never written to disk.

## Publishing a new version

```sh
cd intellij
# 1. Bump version in build.gradle.kts AND plugin.xml
# 2. Update <change-notes> in plugin.xml
# 3. Run the publish task:
export JAVA_HOME=~/.local/jdk/jdk-21.0.5+11/Contents/Home
./gradlew publishPlugin
```

The task:
1. Builds the plugin .zip
2. Signs it with the bundled zip signer
3. Verifies it against IntelliJ 2024.3
4. Uploads to plugins.jetbrains.com via the Marketplace REST API
5. Bumps the version row visible to users

JetBrains' compatibility verifier runs server-side after upload —
expect a 5-15 minute review window before the new version is
broadcast to users.

## Releasing to a channel (pre-release / EAP)

Set `JETBRAINS_MARKETPLACE_CHANNEL=eap` before running publishPlugin
to push to the EAP channel instead of stable. Users must opt into
EAP via the channel URL on the plugin's marketplace page.

```sh
export JETBRAINS_MARKETPLACE_CHANNEL=eap
./gradlew publishPlugin
```

## Token security

- The token IS a credential — protect it like a password.
- If pasted anywhere it shouldn't be (chat logs, screen shares,
  git history), **rotate it immediately** at
  https://plugins.jetbrains.com/author/me/tokens — revoke the
  exposed one and generate a new one.
- The build.gradle.kts only reads from env var. Never hardcode.
- `.env` files containing the token must be gitignored.
