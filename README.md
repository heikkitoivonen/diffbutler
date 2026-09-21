# DiffButler

A terminal dashboard for human-directed AI code reviews on GitHub.

Keep track of what each pull request needs next: AI review, your review, or author
changes. Start an AI review, discuss its findings in the agent's own session, and
finish your review in the browser.

![DiffButler dashboard showing pull requests and the next review action](diffbutler.png)

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git.
From a local checkout of this repository, run:

```sh
uv tool install --python 3.14 .
```

uv manages Python and dependencies in an isolated environment. No virtual environment
activation is needed. If `diffbutler` is not on your PATH, run `uv tool update-shell`
and restart your shell.

Install and sign in to Codex CLI or Claude Code before requesting AI reviews.
The native presets target Codex CLI 0.155.1 or Claude Code 2.1.278 on Linux and macOS;
see [supported agents and validation](docs/native-reviews.md) for details.

## Use

From the GitHub repository you want to review, launch:

```sh
diffbutler
```

First-run setup detects the repository, asks for your GitHub username and preferred
agent, and saves your choices. Public repositories can use anonymous access. For a
private repository, set a GitHub token in an environment variable before launching;
setup asks for the variable's name and validates access. It never saves the token.
Later launches reuse your configuration.

Select a PR with the arrow keys, then work through the review:

1. Press `r` to start a background AI review. Reviews start only when you request them.
2. Press `Enter` to read the results, or `v` to resume the saved agent conversation.
   Ask questions and explicitly tell the agent which comments to post, if any.
3. Exit the agent to return to the dashboard, which refreshes activity from GitHub.
   Press `w` if you are waiting for author changes, or `o` to review in your browser.
4. After approving on GitHub, press `m` to mark the task complete locally. DiffButler
   also detects your approval, closure, or merge and cleans up owned session resources
   while retaining review history.

`Space` performs the selected PR's next action: start a review, discuss findings,
or open the browser. An AI review never counts as your approval. Background analysis
uses the agent's native read-only controls; source sent to the agent may go to its
model provider.

- `/`: Filter the queue.
- `a`: Toggle monitoring all open PRs in configured repositories.
- `b`: Browse accessible PRs and follow one.
- `R`: Refresh from GitHub.
- `h`: View completed review history.
- `?`: Show all keyboard shortcuts.
- `q`: Quit.

See the [dashboard guide](docs/dashboard.md) for all actions and
[configuration guide](docs/configuration.md) for settings.

## Reset

Close running DiffButler sessions, then run:

```sh
diffbutler reset
```

After confirmation, this permanently removes DiffButler's standard configuration,
data, and state directories, including review history, saved sessions, and diagnostic
logs. Custom files outside those directories are preserved. Use `diffbutler reset --force`
to skip confirmation. Run `diffbutler` again to repeat first-run setup.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and checks, and
[SECURITY.md](SECURITY.md) for security information.

## License

[MIT](LICENSE.txt).
