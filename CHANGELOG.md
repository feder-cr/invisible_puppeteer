# Changelog

## [Unreleased]

## [0.2.0] - 2026-10-06

### Changed
- **The language of `locale="auto"` is the real Firefox of the egress
  country, by share.** Through `invisible-core` 36.33.0 (the required version
  is exact, as before). A country used to map to one country tag (`de-DE`)
  that no Firefox build has, so a German egress sent `de-DE,de;q=0.9,...`
  where a German Firefox sends `de,en-US;q=0.9,en;q=0.8`, and every session
  of a country spoke the same language. Now each country carries the Firefox
  builds people there run and their share, from Mozilla's Firefox Public
  Data Report (Belgium: French 40%, Dutch 31%, US English 22%), and the egress
  IP picks one: the same address always gets the same language, and the
  addresses of a country spread over its builds as measured.
- **An explicit `locale` is `navigator.language`.** `locale="en-AU"` reports
  `en-AU, en-US, en` (it reported `en-US, en`), and `"fr-FR"` reports
  `fr-FR, fr, en-US, en` (it reported `fr` first): the requested tag goes
  first and Firefox's own table gives the tail.

## [0.1.3] - 2026-10-04

### Changed
- **A session reports one language in every value.** `locale="auto"` and an
  explicit tag both go through `invisible_core.prepare_session_geo`, the same
  call that resolves the timezone, and the session keeps its result: the
  language list Firefox's own table gives that tag. `navigator.language`,
  `navigator.languages`, the locale prefs, the default context's locale, the
  `Accept-Language` header and the Google CONSENT cookie all read it. A region
  that has no Firefox build of its own used to report two languages: an
  Australian egress resolved `en-AU` for the locale prefs and `en-US, en` for
  the language list, so `navigator.language` said `en-US` while the requested
  locale said `en-AU`. It now reports `en-US` everywhere, as an English
  Firefox installed in Australia does, and the same holds for the other
  regions in that position (New Zealand, Ireland, India and more).
- **The session's default context carries the decided language**, the first
  entry of the list (`fr` for a French session, whose list is
  `fr, fr-FR, en-US, en`), not the tag that was passed.
- **The persona cookies (`prep_recaptcha=True`) come from the core**
  (`invisible_core.persona_cookies`). Same cookies for the same seed; the
  CONSENT cookie's language reads the decided language instead of the raw
  tag. The private `_recaptcha_seed` module is gone.

### Fixed
- **A context's locale reaches the engine as the language list it stands
  for.** `Browser.setLocaleOverride` carried the bare tag, so on firefox-36,
  which applies a context's locale, a context asking for `de-DE` would report
  `navigator.languages == ["de-DE"]` where a German Firefox reports
  `de-DE, de, en-US, en`, and the default context would drop from the
  profile's four entries to one. The list is the core's decision for the tag
  (`decide_session_locale(tag).accept_languages`).

### Requires
- `invisible-core` 36.32.0, which seals the firefox-36 engine and makes the
  language decision. This version does not run on an earlier core.

## [0.1.2] - 2026-10-03

### Changed
- **Values a user picks are committed by Firefox itself.** `page.select` and
  `ElementHandle.uploadFile` hand the choice to the engine's native input
  commands (`Page.selectOptions`, `Page.setFileInputFiles`), which take the
  same paths as the dropdown and the file picker. The page gets `input` and
  `change` with the shape a user's pick gives, inside shadow roots too, and
  nothing when the selection did not change, where pyppeteer fires the pair
  regardless. The command this used to call, `Page.dispatchTrustedInputEvents`,
  is gone from the engine, so this needs the engine that ships the new commands
  and lands with the core pin that seals it.

### Fixed
- **A click lands where it was asked on a page with a saved zoom**, and on
  Linux behind a SOCKS proxy real sites see WebRTC working: both are fixes in
  the firefox-35 engine.

### Requires
- `invisible-core` 35.32.0, which seals the firefox-35 engine. This version
  does not run on firefox-34, and earlier versions do not run on firefox-35.

## [0.1.1] - 2026-10-02

### Fixed
- **On Linux, a headless session starts on its own virtual display.** The
  display counted as ready as soon as its lockfile existed, which Xvfb writes
  before it opens its sockets, so sessions started together could land on
  each other's display and the browser failed with `cannot open display`.
  The display is also no longer reachable over TCP.

### Requires
- `invisible-core` 34.32.0, which carries the display fix. Same engine
  (firefox-34).

## [0.1.0] - 2026-09-25

First version: a replica of invisible_playwright 0.25.7 with pyppeteer's contract.

- `launch()` starts the same patched Firefox with the same profile, proxy,
  geography and hidden-surface handling as `InvisiblePlaywright`, and drives it
  over Juggler: no Chromium, no DevTools protocol, `navigator.webdriver` false.
- pyppeteer's public API: `Browser`, `BrowserContext`, `Page`, `Frame`,
  `ExecutionContext`, `JSHandle`, `ElementHandle`, `Keyboard`, `Mouse`,
  `Touchscreen`, `Dialog`, `ConsoleMessage`, `Request`, `Response` and the
  exceptions, with the same coroutines and events.
- Every click, hover and mouse move travels along a path drawn from the session
  seed; keys carry the session's typing rhythm; input events are trusted.
