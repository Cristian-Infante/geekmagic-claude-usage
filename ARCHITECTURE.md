# Architecture

The code lives in the `geekmagic` package. `tray.py` and `geekmagic_usage.py` at the top are one-line launchers (the
start-at-login entry runs `tray.py`).

## Layers

Dependencies point downwards only. Everything also uses `model.py` (the typed `Usage`) and `errors.py`, so they're
left out of the drawing, as are arrows that other arrows already imply (`app` also imports `insights` directly).
A test (`tests/test_architecture.py`) fails if a lower layer reaches up.

```mermaid
flowchart TD
    entry["tray.py<br/>geekmagic_usage.py"]:::entry
    app["app<br/>the tray app"]:::top
    cli["cli<br/>one-shot command line"]:::top
    providers["providers<br/>WHAT is read"]:::mid
    render["render<br/>how it LOOKS"]:::mid
    device["device<br/>the SCREEN"]:::mid
    insights["insights<br/>what the numbers MEAN"]:::low
    system["system<br/>the OS"]:::low

    entry --> app
    entry --> cli
    app --> providers
    app --> render
    app --> device
    app --> system
    cli --> providers
    cli --> render
    cli --> device
    providers --> insights
    providers --> system
    render --> insights

    classDef entry fill:#eee,stroke:#888
    classDef top fill:#dbeafe,stroke:#3b82f6
    classDef mid fill:#dcfce7,stroke:#22c55e
    classDef low fill:#fef9c3,stroke:#eab308
```

| Folder | Role | Knows about |
|---|---|---|
| `app/` | Keeps the screen up to date: the tray icon, menu and the worker loop | everything below |
| `cli.py` | Pushes one provider's usage once or on a loop | providers, render, device |
| `providers/` | One class per AI tool: read its usage, find its CLI, count its logs, sign in | insights, system |
| `render/` | Readings in, pictures out: mascots, animations, one class per screen | insights |
| `device/` | The SmallTV's web API and finding it on the network | nothing |
| `insights/` | Pure logic: pace projection, alert thresholds, activity from local logs | nothing |
| `system/` | What depends on the OS: notifications, lock detection, start at login, terminals | nothing |

## Inside `app/`

`TrayApp` only builds the parts and wires them together; each part has one job and gets what it needs in its constructor.

```mermaid
flowchart TD
    tray["TrayApp<br/>builds and wires, handles clicks"]:::root
    sched["Scheduler<br/>the worker loop"]
    usage["UsageService<br/>reads providers, pace, alerts"]
    screen["Screen<br/>the device and what is stored on it"]
    agents["AgentMonitor<br/>is an agent working?"]
    activity["ActivityCounter<br/>stats from local logs"]
    signin["SignInCoordinator<br/>signed-out providers"]
    side["Backlight, Power,<br/>Notifications, Persistence"]

    tray --> sched
    tray --> side
    sched --> usage
    sched --> screen
    sched --> agents
    sched --> activity
    sched --> signin

    classDef root fill:#dbeafe,stroke:#3b82f6
```

## The two families of interchangeable classes

A new AI tool is a new `Provider`; a new screen is a new `View`. The rest of the app doesn't change.

```mermaid
classDiagram
    class Provider {
        <<interface>>
        fetch() Usage
        find_cli() path
        local_stats() dict
        agent_state() dict
        login_args
    }
    Provider <|-- ClaudeProvider
    Provider <|-- CodexProvider

    class View {
        <<interface>>
        render(data, animation, rotation) Rendered
    }
    View <|-- SingleView
    View <|-- SplitView
    View <|-- StatsView
    View <|-- BreakdownView
    View <|-- HoursView
    View <|-- ErrorView
```

## One update cycle

What the worker does each time (every 30 s, or when a click or an agent change wakes it):

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
    U-->>S: Usage (with pace, alerts done)
    S->>C: deliver(view, data)
    C->>V: render(data)
    V-->>C: GIF and rotation picks
    C->>D: upload(GIF)
    C->>D: show_image(file)
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
