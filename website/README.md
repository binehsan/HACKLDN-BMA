PanikBot website (docs)

This folder contains the static landing page for PanikBot. It is ready to be published via GitHub Pages.

How to publish (two options):

1) Serve `website/` as the project pages (docs folder)
   - Rename/move the contents into a `docs/` folder at repo root, or set the GitHub Pages source to `/docs` in repository settings.
   - Commit and push. GitHub Pages will serve the site at `https://<your-org-or-username>.github.io/<repo>/`.

2) Publish from the `gh-pages` branch
   - Use your preferred deploy action to push the contents of `website/` to the `gh-pages` branch.
   - GitHub Pages settings: choose `gh-pages` as the publishing source.

Notes
- The page currently shows a "coming soon" state and has no external invite links enabled.
- Logo is referenced from `../assets/logo.png` in the repository; keep that file in place.
- The demo iframe points to `../test2.html`. Replace with a hosted example when ready.

Quick preview locally
- You can preview the site by opening `website/index.html` in your browser, or use a small static server:

  Python 3.x:

  ```powershell
  cd website
  python -m http.server 8000
  # then open http://localhost:8000
  ```

Security note
- Do not publish any secrets or `.env` files. The repository contains credentials that should be removed before making the repo public.
