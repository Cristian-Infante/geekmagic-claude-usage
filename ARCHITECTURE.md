# Architecture

The code lives in the `geekmagic` package. `tray.py` and `geekmagic_usage.py` at the top are one-line launchers (the
start-at-login entry runs `tray.py`).

```mermaid
flowchart LR
    subgraph top["entry points"]
        tray["tray.py"]
        cli["geekmagic_usage.py"]
    end
    subgraph base["shared"]
        model["model.py<br/>Usage, Window, Projection,<br/>CodexExtra, ErrorScreen"]
        errors["errors.py<br/>UsageError, SignInNeeded"]
        paths["paths.py"]
    end
    subgraph providers["providers/ : WHAT is read"]
        pbase["Provider (interface)"]
        pclaude["ClaudeProvider"]
        pcodex["CodexProvider"]
        pbase --> pclaude
        pbase --> pcodex
    end
    subgraph insights["insights/ : what the numbers MEAN"]
        pace["pace.py"]
        alerts["alerts.py"]
        stats["usage_stats.py"]
        activity["agent_activity.py"]
    end
    subgraph render["render/ : how it LOOKS"]
        views["views/<br/>Single, Split, Stats,<br/>Breakdown, Hours, Error"]
        look["palette, mascots,<br/>animations, components, gif"]
        views --> look
    end
    subgraph device["device/ : the SCREEN"]
        client["GeekMagicDevice"]
        discovery["discovery.py, files.py"]
    end
    subgraph system["system/ : the OS"]
        sysmods["notifier, session_lock,<br/>autostart, single_instance,<br/>login, executables"]
    end
    subgraph app["app/ : the tray app"]
        tray_app["TrayApp<br/>(composition root)"]
        services["Scheduler, UsageService, Screen,<br/>AgentMonitor, ActivityCounter,<br/>Backlight, Power, SignInCoordinator,<br/>Notifications, Persistence"]
        tray_app --> services
    end
    tray --> tray_app
    cli --> client
    cli --> views
    services --> pbase
    services --> views
    services --> client
    services --> sysmods
    services --> insights
```

## Layers

Dependencies point downwards only: `app` uses everything; `providers`, `render` and `device` don't know about
`app`; `render` knows nothing of providers or the network; `system` and `device` know nothing of the rest. A test
(`tests/test_architecture.py`) keeps it that way.

```mermaid
flowchart TD
    app["app"] --> providers
    app --> render
    app --> device
    app --> insights
    app --> system
    cli["cli"] --> providers
    cli --> render
    cli --> device
    providers --> insights
    providers --> system
    render --> insights
    providers --> model["model, errors"]
    render --> model
    insights --> model
    device --> model
    system --> paths["paths"]
```

## One update cycle

What the worker does each time (30 s by default, or when a click or an agent change wakes it):

```mermaid
sequenceDiagram
    participant S as Scheduler
    participant U as UsageService
    participant P as Provider
    participant C as Screen
    participant V as View
    participant D as GeekMagicDevice
    S->>U: fetch(provider)
    U->>P: fetch()
    P-->>U: Usage
    U->>U: history, pace, alerts
    U-->>S: Usage
    S->>C: deliver(view, data)
    C->>V: render(data, animation, rotation)
    V-->>C: Rendered (GIF + picks)
    C->>D: upload(GIF, spare slot)
    D-->>C: stored
    C->>C: remember picks, swap slot
    C->>D: show_image(file) if still the active view
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
| **Value objects** | `model.py` (`Usage`, `Window`...), `render/views/Rendered` | A reading is a typed object, not a dict, so a misspelt field fails at once; a view returns the GIF *and* the rotation picks to remember, and the caller records them only once the upload went through. |

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
