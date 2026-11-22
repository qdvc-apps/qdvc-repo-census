# macOS Local Network privacy blocked iTerm2 while Settings showed it as allowed

- **Date encountered:** 2026-11-22
- **Reported by:** Project owner (on the Mac) with Claude Opus 5.5 (Anthropic), in a claude.ai session
- **Project visibility:** Public
- **Project:** qdvc-repo-census
- **Platform:** Python, the shell and the terminal (`python-shell`). The trap is a macOS privacy control that hits programs started from third-party terminal apps. If maintainers would rather file it elsewhere, a new "macOS platform: privacy and permissions" section would fit it, next to the Android 17 local-network entry.
- **Versions:** macOS 15 or later (Local Network privacy first appeared in macOS 15); exact macOS version unknown. iTerm2 version unknown. Python 3 (exact version unknown), standard library `urllib`/`socket`. qdvc-repo-census 0.1.0–0.1.1.
- **Log link:** Pending: qdvc-repo-census, `<log-path>`, UR.2 (symptom) and UR.3 (workaround)
- **Status:** Workaround only. It was confirmed to work on the affected Mac. The underlying cause is a best guess.
- **Related entry:** None. Compare "Targeting Android 17, local network connections just time out", the same kind of permission on Android.

## Symptom

`repocensus` checks each Git server's homepage before fetching. On one Mac it reported a LAN server, along the lines of `https://charlie9:3030/`, as offline. Internet-hosted servers were fine, and so was the same LAN server from Linux.

On the Mac it worked in Terminal.app but not in iTerm2. iTerm2 was already listed and switched on under System Settings > Privacy & Security > Local Network.

The exact error text wasn't captured. Apple's documentation and forum threads say a connection blocked by Local Network privacy fails with `EHOSTUNREACH`, which Python reports as `[Errno 65] No route to host`. There's no permission prompt and no error naming a permission. Whether `git fetch` itself also failed when run from iTerm2 wasn't checked.

## Cause

There are two parts:

1. **Documented (Apple TN3179 and Apple DTS forum answers):** On macOS 15+, connections to the local network need the Local Network permission. Terminal.app, and every program it starts, is automatically exempt. Other terminal apps such as iTerm2, VS Code's terminal and Warp aren't. The permission belongs to the terminal app, and command-line tools started from it inherit its state. So the same Python script works in Terminal.app and fails in iTerm2.
2. **Best guess, not measured:** The permission state for iTerm2 was stale or out of sync. The toggle showed it as allowed, but connections were still being blocked. Apple developer forum threads describe granted permissions spontaneously failing later with `EHOSTUNREACH`. Apple DTS has also said there's no good way to reset the local network privacy state on macOS. It's not known which step of the workaround mattered: switching off, relaunching, or switching back on.

## Fix

This workaround was confirmed to work:

1. Quit iTerm2.
2. In System Settings > Privacy & Security > Local Network, switch iTerm2's toggle **off**.
3. Open iTerm2, run any command, and quit it again.
4. Switch iTerm2's toggle back **on**.
5. Open iTerm2. LAN connections then worked normally.

A tempting dead end: the AI's first theory was a different macOS-only difference. When no proxy environment variable is set, Python's `urllib.request.getproxies()` falls back to the proxy in System Settings, which git ignores. That behaviour is real, and the tool now ignores OS proxy settings to match git (0.1.1). But it wasn't the cause here. What led to the answer was the "no route to host" Local Network hint added in the same release, and that made "try it in Terminal.app" the decisive test.

In the tool (0.1.2), "no route to host" on macOS, from either the homepage check or `git fetch`, now produces a hint. The hint names the terminal app (from `TERM_PROGRAM`), says Terminal.app is exempt, and describes the toggle-off-and-on workaround. No hint is shown when running in Terminal.app, where the error is probably a real routing problem.

## Watch for

- **Any non-Terminal launcher of a networked command-line tool on macOS 15+:** iTerm2, VS Code or JetBrains terminals, Warp, Ghostty, IDE run buttons, launch agents, cron. "Works in Terminal.app, fails elsewhere" is the signature.
- **Quick test:** run the same command in Terminal.app. If it works there, suspect Local Network privacy before suspecting DNS, firewalls, proxies or the server.
- **"Allowed" in Settings doesn't guarantee it's in force.** If the app is already switched on and connections still fail with `EHOSTUNREACH`, try the off, relaunch, on cycle.
- **What it affects:** only local-network destinations, so internet hosts keep working. That makes it look like the LAN server is down.
- **Health checks:** a connectivity check that treats `EHOSTUNREACH` as "server offline" will report LAN servers as down. Show a distinct "possibly blocked by Local Network privacy" state instead, as the Android 17 entry recommends for its equivalent.

## Minimal example

```python
# Run from iTerm2 (blocked) and from Terminal.app (exempt) on macOS 15+.
import socket
socket.create_connection(("192.168.1.20", 3030), timeout=5)
# Blocked: OSError: [Errno 65] No route to host
# Allowed: connects (or fails with a normal refusal/timeout)
```

Sources:

- [TN3179: Understanding local network privacy](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy) (Apple)
- [No route to host](https://developer.apple.com/forums/thread/782936) (Apple Developer Forums: Terminal exemption; no good way to reset the state)
- [Local Network Privacy not Working as Documented](https://developer.apple.com/forums/thread/776479) (Apple Developer Forums: the Terminal exemption doesn't extend to other terminal apps)
- [Local Network privacy blocking my app when it shouldn't](https://developer.apple.com/forums/thread/772171) (Apple Developer Forums: granted permission later failing with EHOSTUNREACH)
