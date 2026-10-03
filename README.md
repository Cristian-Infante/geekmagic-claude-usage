# geekmagic-claude-usage

Show your **Claude Code** and **Codex** usage limits — the "current session /
5-hour" and "current week" numbers — on a [GeekMagic](https://geekmagic.cc/)
SmallTV's screen, with an animated pixel-art mascot for each. Click a tray icon
to flip between them. **No firmware reflash required** — it uses the device's
stock, unmodified web API.

<table>
<tr>
  <td align="center"><img src="docs/preview.jpg" width="200"><br><b>Claude</b></td>
  <td align="center"><img src="docs/preview-codex.jpg" width="200"><br><b>Codex</b></td>
</tr>
</table>

## How it works

Both providers go through the same pipeline; only step 1 differs.

1. **Read the usage** from the provider's own CLI, using the login you already have:

   | | Claude | Codex |
   |---|---|---|
   | Command | `claude -p --safe-mode --output-format json --max-budget-usd 0.000001 --tools "" --no-session-persistence --no-chrome /usage` | `codex app-server` → `account/rateLimits/read` ([docs](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt)) |
   | What it is | Claude Code's built-in `/usage`, run in a locked-down zero-tool mode. The result is checked for `total_cost_usd == 0` before it's trusted. | The official account-limits call. It doesn't start a model turn. |
   | Cost | **Never spends a token.** | **Never spends a token.** |
   | Needs | Claude Code installed and logged in | Codex CLI installed and signed in with `codex login` using ChatGPT (an API-key-only login has no plan limits) |

2. **Render** a 240×240 animated GIF with [Pillow](https://python-pillow.org/):
   the mascot doing something (see [Animations](#animations)) plus two usage
   bars with their reset countdowns. Claude is orange/gold with a little
   creature; Codex is blue/violet with a cloud, so you can tell them apart at a glance.
3. **Upload** it straight to the device over HTTP (`POST /doUpload?dir=/image/`),
   switch the device to its Photo Album theme (`GET /set?theme=3`) and pin the
   GIF (`GET /set?img=/image/<file>`).

No custom firmware, no soldering, nothing installed on the device — just a few
plain HTTP calls to the web server it already runs.

## Requirements

- A GeekMagic SmallTV running the stock **[GeekMagicClock/smalltv-ultra](https://github.com/GeekMagicClock/smalltv-ultra)**
  firmware (confirm by opening `http://<device-ip>/settings.html` — the page
  links back to that repo). Other GeekMagic firmware families (SD_RU/SD Pro
  community firmware, the ESP32 "PRO" `.sys` variant) use a **different** API
  and are not supported as-is.
- [Claude Code](https://claude.ai/code) and/or the Codex CLI, installed and
  logged in on the machine that runs this. You only need the one(s) you want to see.
- Python 3.11+ and [Pillow](https://python-pillow.org/). For the tray app also
  `pystray` (macOS: plus `pyobjc`; Windows: plus `tzdata`).
- The device and this machine on the same local network. Give the device a
  fixed IP (a DHCP reservation in your router) so it keeps working after it restarts.

## Quick start

```bash
git clone https://github.com/RubenU2002/geekmagic-claude-usage.git
cd geekmagic-claude-usage
pip install Pillow pystray      # macOS: also pyobjc · Windows: also tzdata

# find your device's IP (your router, or the device's own screen/menu)
python tray.py --ip 192.168.1.18
```

That starts the [tray app](#tray-app): it keeps the screen updated every 30 s
and gives you an icon to switch between Claude and Codex. No tray? Run a single
provider from the command line instead:

```bash
python geekmagic_claude.py --ip 192.168.1.18                       # Claude, once
python geekmagic_claude.py --ip 192.168.1.18 --provider codex      # Codex, once
python geekmagic_claude.py --ip 192.168.1.18 --loop 60             # repeat every 60 s
```

`codex` is looked up on `PATH`, then in `~/.local/bin`, `/opt/homebrew/bin`,
`/usr/local/bin`, then the copy bundled with the VS Code / Cursor ChatGPT extension.
If a query fails, it retries at the next interval and reports the error in the
console or the tray tooltip; the device keeps its previous image.

## Tray app

`tray.py` keeps the screen updated (every 30 s; `--interval` changes it) and
adds an icon to switch provider.

| | Windows / Linux | macOS |
|---|---|---|
| Switch provider | left-click the icon | click the menu-bar icon, choose Claude or Codex |
| Menu | right-click: Claude, Codex, Update now, View logs, Quit | same, from the same click |

- **Near-instant switching.** Each provider's last image already lives on the
  device (and is remembered across restarts), so a click only changes which one
  is shown; fresh data uploads in the background, and a click cancels any
  upload in flight.
- **Screen off or unreachable?** It retries every ~10 s and shows the image as
  soon as the device answers.
- **View logs** opens `tray.log` (next to the script, rotated at 512 KB).

### Start it at login (any OS)

```bash
python tray.py --ip 192.168.1.18 --install-startup   # creates the right thing for your OS
python tray.py --uninstall-startup
```

- **Windows:** a Task Scheduler task (`GeekMagicClaude`) that runs at logon, windowless.
- **macOS:** a LaunchAgent `~/Library/LaunchAgents/com.geekmagic.claude-usage.plist`
  (restarts after a crash, not after you choose Quit; sets a `PATH` that
  includes Homebrew and `~/.local/bin` so `claude`/`codex` are found).
- **Linux:** an autostart entry `~/.config/autostart/geekmagic-claude-usage.desktop`
  (needs a desktop with a system tray / AppIndicator).

## Animations

By default (`--animation auto`) every push picks a random animation from the
provider's own set, never one of the last three that played. Pass
`--animation NAME` to pin one instead (`random` is the same as `auto`). All of
them live side by side in the `ANIMATIONS` dict in `geekmagic_claude.py` —
picking one never deletes another, and adding a new one is just a new entry.

```bash
python geekmagic_claude.py --ip 192.168.1.18 --animation coffee
python geekmagic_claude.py --ip 192.168.1.18 --provider codex --animation bolt
```

### Claude

<table>
<tr>
  <td align="center"><img src="docs/preview-idle.gif" width="160"><br><code>idle</code></td>
  <td align="center"><img src="docs/preview-laptop.gif" width="160"><br><code>laptop</code></td>
  <td align="center"><img src="docs/preview-coffee.gif" width="160"><br><code>coffee</code></td>
</tr>
<tr>
  <td align="center">Just the mascot's gentle idle bob</td>
  <td align="center">Pulls a tiny laptop up into view and holds it</td>
  <td align="center">Pulls out a mug of coffee, with steam wisps rising</td>
</tr>
<tr>
  <td align="center"><img src="docs/preview-eureka.gif" width="160"><br><code>eureka</code></td>
  <td align="center"><img src="docs/preview-dance.gif" width="160"><br><code>dance</code></td>
  <td align="center"><img src="docs/preview-typing.gif" width="160"><br><code>typing</code></td>
</tr>
<tr>
  <td align="center">A little idea spark pops up beside its head</td>
  <td align="center">A side-to-side wiggle dance</td>
  <td align="center">Like <code>laptop</code>, plus a moving cursor on the little screen</td>
</tr>
</table>

### Codex

<table>
<tr>
  <td align="center"><img src="docs/preview-codex-prompt.gif" width="160"><br><code>prompt</code></td>
  <td align="center"><img src="docs/preview-codex-think.gif" width="160"><br><code>think</code></td>
  <td align="center"><img src="docs/preview-codex-sparkle.gif" width="160"><br><code>sparkle</code></td>
</tr>
<tr>
  <td align="center">The <code>&gt;_</code> appears, the cursor blinks, three dots get typed</td>
  <td align="center">The cloud bobs while thinking dots come and go</td>
  <td align="center">Sparkles twinkle around the cloud</td>
</tr>
<tr>
  <td align="center"><img src="docs/preview-codex-bolt.gif" width="160"><br><code>bolt</code></td>
  <td align="center"><img src="docs/preview-codex-code.gif" width="160"><br><code>code</code></td>
  <td align="center"><img src="docs/preview-codex-hop.gif" width="160"><br><code>hop</code></td>
</tr>
<tr>
  <td align="center">The cloud flashes and a lightning bolt strikes</td>
  <td align="center"><code>[ ... ]</code> brackets appear with dots typed inside</td>
  <td align="center">The cloud hops, its shadow shrinking as it rises</td>
</tr>
</table>

### Adding your own

Each animation is a tuple of `(mascot_dx, mascot_dy, extra)` per GIF frame,
where `extra` is an optional `(image, mascot_width) -> None` drawing hook
(see `_laptop_layer`, `_mug_layer`, `_bulb_layer` for Claude and
`_prompt_layer`, `_bolt_layer`, `_hop_layer` for Codex). Chain several hooks
with `_combine(...)`. Define a small bitmap (a tuple of equal-length strings,
one character per pixel), a tiny drawing function, a layer closure, add the
key to `ANIMATIONS`, and list it in `ANIMATION_GROUPS` under the provider it
belongs to. A new provider is a new entry in `PROVIDERS` (how to read its
usage) and `THEMES` (its mascot and colours).

## Running it without the tray (macOS launchd loop)

The included `com.example.geekmagic-claude.plist` is a
[launchd](https://www.launchd.info/) LaunchAgent that runs the plain command
line script in the background, at login and on a timer. (The tray app's
`--install-startup` is simpler if you want the tray.)

1. Copy it and fill in the placeholders:
   ```bash
   cp com.example.geekmagic-claude.plist ~/Library/LaunchAgents/com.yourname.geekmagic-claude.plist
   ```
   Edit that copy:
   - `Label` — make it match the filename (e.g. `com.yourname.geekmagic-claude`)
   - The `python3` path — run `which python3` and use that exact path
   - The path to `geekmagic_claude.py` — wherever you cloned this repo
   - `--ip` — your device's IP (and `--provider codex` for Codex)
   - Both `StandardOutPath`/`StandardErrorPath` — anywhere you want the log written

2. Load it:
   ```bash
   launchctl load ~/Library/LaunchAgents/com.yourname.geekmagic-claude.plist
   ```

It runs once immediately (`RunAtLoad`) and every 60 seconds (`StartInterval`)
while you're logged in, and restarts after a reboot or logout/login.

```bash
tail -f /path/to/your/log.txt                                            # watch it work
launchctl unload ~/Library/LaunchAgents/com.yourname.geekmagic-claude.plist   # pause
launchctl load   ~/Library/LaunchAgents/com.yourname.geekmagic-claude.plist   # resume
```

**Gotchas**

- If your script directory is under `~/Downloads`, `~/Desktop`, or
  `~/Documents`, macOS's privacy protection (TCC) can block a launchd job from
  reading it with a silent `Operation not permitted`, even though running it by
  hand works (Terminal already has disk access; launchd doesn't). Move the
  folder elsewhere (e.g. `~/Library/Application Support/geekmagic-claude-usage`).
- launchd does not load your shell's `PATH`, so if `claude` or `codex` lives
  in `/opt/homebrew/bin` (Homebrew on Apple Silicon), add it to the plist's
  `EnvironmentVariables` (already done in the example file).
- A repeating "`python3.13` wants access to data from other apps" prompt is
  Claude Code's "Claude in Chrome" integration reaching for Chrome via Apple
  Events on every run. `fetch_usage()` already passes `--no-chrome`, which
  stops it.

Other platforms: use `cron`, a `systemd --user` timer, or Windows Task
Scheduler to run `python3 geekmagic_claude.py --ip ... [--provider codex]` on a
schedule — or just use the tray app's `--install-startup`.

## Firmware API reference (stock SmallTV Ultra)

Discovered by probing a real device; documented here since GeekMagic's own
docs don't cover it:

| Action | Endpoint |
|---|---|
| Upload an image/GIF | `POST /doUpload?dir=/image/` (multipart field `file`; same filename overwrites) |
| Pin it on screen | `GET /set?img=/image/<filename>` (requires Photo Album theme) |
| Switch to Photo Album | `GET /set?theme=3` |
| Delete a file | `GET /delete?file=/image/<filename>` |
| List files | `GET /filelist?dir=/image/` (HTML table) |
| Free space | `GET /space.json` |

Animated GIFs are decoded and looped **on the device itself** — one upload per
push, no per-frame network traffic. The device handles one request at a time
and takes ~35 ms per KB to store a file (a ~80 KB GIF keeps it busy for about
3 s), which is why the tray app uploads each provider into two alternating
files (`<provider>-usage-a.gif` / `-b.gif`) and switches providers with just a
`/set?img=` call. Keep GIFs well under the device's free space
(`/space.json`); these run 25–95 KB.

## Why this exists / prior art

A few public projects inspired pieces of this one:

- [MyrikLD/GeekMagic-Clawdmeter](https://github.com/MyrikLD/GeekMagic-Clawdmeter) —
  custom Rust firmware for the SmallTV PRO that reads Anthropic's
  undocumented `/api/oauth/usage` endpoint directly over WiFi. Closest in
  spirit, but it **replaces** the stock firmware entirely.
- [hsmloktar/geekmagic-ai-usage-monitor](https://github.com/hsmloktar/geekmagic-ai-usage-monitor) —
  the source of the "shell out to the local CLI's own usage command"
  approach used here, generalized across Codex and Claude.
- [epicsagas/AgentGlance](https://github.com/epicsagas/AgentGlance) — a
  Claude Code plugin that turns a GeekMagic SmallTV into a live
  WORKING/APPROVAL/DONE session-status display (context %, tokens) rather
  than an account-wide quota display; also the source of several of the
  stock-Ultra firmware API details documented above.

This project takes a different, narrower path: no firmware changes, no OAuth —
just each provider's own local CLI and a few plain HTTP calls to the stock device.

## Disclaimer

The mascots are original pixel-art designs made for this project — the little
creature is **not** an official Anthropic/Claude logo or asset, and the cloud
is **not** an OpenAI/Codex logo or asset. This project is unaffiliated with
Anthropic, OpenAI and GeekMagic; it talks to each of their products only
through the interfaces they already expose (Claude Code's local `/usage`
command, the Codex CLI's account-limits call, and the device's own stock HTTP API).

## License

MIT — do whatever you want with it.
