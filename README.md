# SatNOGs_Integration

Integrating SatNOGs network with the ground station

run `uv run pytest` for test suite

## Running as a service

A service manager keeps the program running and keeps its logs. The program logs to stderr and
needs no changes for this. Run only one copy per API key.

Exit codes: 0 ok, 1 sweep failed (`--once`), 2 bad config, 3 key refused, 130 ctrl-c.

### macOS (launchd)

Run `uv sync` first. Then, from the repo directory:

```bash
sed "s|REPO_DIR|$PWD|; s|UV_BIN|$(command -v uv)|; s|HOME_DIR|$HOME|g" deploy/io.brownspace.satnogs-integration.plist > ~/Library/LaunchAgents/io.brownspace.satnogs-integration.plist
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/io.brownspace.satnogs-integration.plist
```

- Logs: `tail -f ~/Library/Logs/satnogs-integration.log`
- State: `launchctl print gui/$UID/io.brownspace.satnogs-integration`
- Stop and remove: `launchctl bootout gui/$UID/io.brownspace.satnogs-integration`
