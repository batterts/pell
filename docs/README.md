# pell — documentation source

This directory holds the source for the pell documentation site.

- **Live site:** <https://batterts.github.io/pell/>
- **Local serve:** `bundle exec jekyll serve` from this directory, or
  `(cd docs && jekyll serve)` from the repo root. Requires a Ruby
  toolchain (`gem install bundler jekyll just-the-docs`).

GitHub Pages is configured to deploy this directory automatically:

- Settings → Pages → Source: **Deploy from a branch**
- Branch: **`main`** / Folder: **`/docs`**

Push to `main` and the site rebuilds in ~30s.

## Layout

- `_config.yml` — Jekyll + just-the-docs configuration
- `index.md` — landing page
- `tutorial/` — narrative chapters (numbered)
- `reference/` — alphabetical reference pages
- `cookbook/` — task-oriented recipes
- `reviews/` — five-reviewer critique reports
- `benchmarks.md` — bulk-insert benchmark vs. raw PL/SQL

Each page carries `title:`, `parent:`, and `nav_order:` frontmatter so
just-the-docs renders the sidebar correctly. New pages need the same
shape — see any existing tutorial chapter for the pattern.
