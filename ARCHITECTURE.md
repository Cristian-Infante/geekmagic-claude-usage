# Architecture

The code lives in the `geekmagic` package. `tray.py` and `geekmagic_claude.py` at the top are one-line launchers (the
start-at-login entry runs `tray.py`).

```
geekmagic/
  errors.py            UsageError, SignInNeeded
  paths.py             where the app keeps its own files
  providers/           WHAT is read: one module per AI tool, behind one interface
    base.py              Provider (abstract): fetch(), find_cli(), local_stats(), agent_state(), login_args
    claude.py codex.py   ClaudeProvider / CodexProvider (+ the code that reads each one's usage)
    __init__.py          the registry: ALL, PROVIDERS, TITLES
  insights/            what the numbers MEAN (pure logic, no screen, no device)
    pace.py alerts.py    projection of a usage window; alert thresholds
    usage_stats.py       activity counted from the providers' local logs
    agent_activity.py    is an agent working / waiting for you, from its log
  render/              how it LOOKS (no network, no state): readings in, pictures out
    palette.py mascots.py props.py animations.py   colours, pixel-art mascots and what they hold, their animations
    components.py        the pieces screens are drawn from (bars, pills, the pace line, tags)
    gif.py               frames -> the small GIF the device stores
    views/               one class per screen: SingleView, SplitView, StatsView, BreakdownView, HoursView, ErrorView
  device/              the SCREEN on the network
    client.py            GeekMagicDevice: upload, show an image, list/delete, brightness, night mode
    discovery.py         finding it; files.py: the names of the images kept on it
  system/              what depends on the operating system
    notifier.py session_lock.py autostart.py single_instance.py login.py executables.py
  app/                 the tray app: keeps the screen up to date
    tray_app.py          the composition root: builds the parts below, wires them, handles clicks
    scheduler.py         the worker loop
    usage.py             UsageService (read providers), UsageHistory, AlertTracker
    screen.py            Screen: the device's address and state, what's stored on it, uploads
    agents.py            AgentMonitor: per-session tracking of working / waiting / finished
    activity.py          ActivityCounter: stats from the local logs, in the background
    backlight.py power.py signin.py notifications.py
    viewstate.py icons.py menu.py persistence.py state_store.py config.py render_id.py main.py
```

## Layers

Dependencies point downwards only: `app` uses everything; `render`, `device`, `insights` and `providers` don't know about
`app`; `render` doesn't know about the network or the providers; `providers` don't know about screens.

```
        app  ────────────────┐
         │                   │
   ┌─────┼───────┬─────────┐ │
render  device  providers  insights   system
```

## Patterns, and where to look

| Pattern | Where | What it buys |
|---|---|---|
| **Strategy + registry** | `providers/` (`Provider`) | Everything specific to one AI tool is one class; the app only talks to the interface. |
| **Strategy** | `render/views/` (`View.render`) | Each screen is a class with the same `render(data, animation, rotation) -> Rendered`; a new view is a new class and an entry in `PANEL_VIEWS`. |
| **Facade** | `device/client.py` | The device's odd HTTP API (one request at a time, cancellable uploads, theme selection) behind five methods. |
| **Composition root / DI** | `app/tray_app.py` | Services get their collaborators in the constructor (callbacks for what they must tell the app), so each is testable alone. |
| **Observer (hooks)** | `UsageService.on_read/on_failure`, `AgentMonitor.on_change` | A service reports what happened without knowing who cares. |
| **Memento** | `app/persistence.py` | Each part has `restore(saved)` / `snapshot()`; one file remembers them all and a damaged piece only costs that part's memory. |
| **Value object** | `render/views/Rendered` | A view returns the GIF *and* the rotation picks to remember; the caller records them only once the upload went through. |

## Adding a provider

1. `geekmagic/providers/<name>.py`: a `Provider` subclass (read its usage, find its CLI, count its logs, say how to sign in).
2. Add its instance to `ALL` in `providers/__init__.py`.
3. Give it a look: a mascot in `render/mascots.py` (`THEMES`) and animations in `render/animations.py`
   (`ANIMATION_GROUPS`, `WORKING_ANIMATION`, `WAITING_ANIMATION`).
4. Where the screen only has room for two panels the app needs a choice of which two; see the history (commit `506755c`) for
   a worked version with a provider selector and a pair of providers for the views of two.

## Adding a view

A class in `render/views/` with `render(...) -> Rendered`, registered in `PANEL_VIEWS`; a name for the tooltip in
`app/viewstate.py` and an icon in `app/icons.py`; a menu item in `app/menu.py`.
