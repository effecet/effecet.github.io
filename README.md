# effecet.github.io

The project hub for [github.com/effecet](https://github.com/effecet): a single static page with one card per public repo, linking each one's live demo, code and CI status.

**Live:** https://effecet.github.io

## How it works

- `index.html` is the whole site: hand-written HTML and CSS with a little vanilla JS. No build step and no dependencies.
- `assets/` holds the card previews: screenshots of each project's own GitHub Pages demo, plus memory-persistor's graph GIF.
- On load the page reads the public GitHub API to show each repo's last update and star count. If that request fails (rate limit, offline) the page still renders in full.
- It follows the system's light or dark setting; the theme button overrides it per browser.

## Adding a project

Copy an `<article class="card">` block in `index.html`, set `data-repo` to the repo name and `data-tags` to `ai`, `infra` or `fun`, then add a preview to `assets/` or use a `thumb code` block.

## License

MIT © effece
