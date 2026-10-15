# Changelog

## [Unreleased]

## [0.6.3] - 2026-10-15

The pin moves to invisible-core 47.40.0, on the same firefox-47 engine.

### Fixed
- **Closing a browser no longer closes the pipe of another browser in the
  same process.** The connection closed its two descriptor numbers on
  every call, and a released number goes to the next pipe the process
  opens: a second close, or a write racing the close, could reach another
  browser launched by the same script. The connection now releases each
  end once and never touches the number again.

## [0.6.2] - 2026-10-14

The pin moves to invisible-core 47.39.0, which seals the firefox-47 engine.

### Fixed
- **A page can no longer tell that the engine carries the automation protocol.**
  Every engine until firefox-47 registered the protocol as a chrome package named
  `juggler`, and a page tells a registered package from an absent one with one
  line, `new Worker("chrome://juggler/...")`: it returns on our engine and throws
  `SecurityError` on Firefox. Captcha vendors started probing it in October 2026,
  which is why a challenge solved by hand was still refused. firefox-47 loads the
  protocol from `resource://gre/modules/juggler/`, a namespace every Firefox has
  and inside which a page cannot tell which files exist; the engine now registers
  exactly the chrome packages and resource substitutions Firefox 151 does.
- **A `goto` the page stops on its way no longer answers on the old page, nor
  waits out its timeout.** Stopped with nothing in its place
  (`document.open()`, `window.stop()`), it raises PageError
  "NS_BINDING_ABORTED at <url>" at once; replaced by another document, it
  waits on that document, as pyppeteer's NavigatorWatcher does, and answers
  with its response, so the answer always names the page it is on.
- **The wheel is born before the page sees it**, as keys and mouse moves are
  since 0.35.7: `event.timeStamp` was the instant the wheel reached the page,
  0 to 2 ms below `performance.now()` in the handler where a real wheel is 6
  to 14, and half of the wheel events failed the filter a tracker applies.

### Not yet as the original
- Firefox lets the page being left stop a navigation the browser started
  (document.open, window.stop, location.replace, a form submit, an iframe
  navigating the top); pyppeteer's Chromium lets the browser's navigation win
  over all of them but going back, so there `goto` lands on the page asked
  for. The engines differ, and this client answers with Firefox's events.

## [0.6.1] - 2026-10-13

The pin moves to invisible-core 45.39.0, which seals the firefox-45 engine.

### Fixed
- **A key or a mouse move is born before the page sees it**, as on a real
  machine, where the system takes a few milliseconds between the hardware and
  the page's handler. The engine stamped every input event at the instant it
  reached the page, so `event.timeStamp` equalled `performance.now()` there,
  and a script that keeps only the events with `timeStamp < performance.now()`
  dropped most of the keys `keyboard.type` and `page.type` sent. Each event is now born a few milliseconds earlier,
  drawn per event from the session (a median of 6 to 11 ms, fixed by the
  seed): measured in the handler, 4 to 22 ms.
- **The first `navigator.mediaDevices.enumerateDevices()` of a session takes
  the same time on Windows and on Linux**, a time the seed fixes between 240
  and 640 ms. It took the host's audio stack time, 15 to 40 ms on Linux and
  about 300 ms on Windows, so a script that sends its payload before the list
  arrives saw a device field on one build and not on the other. With fake
  devices the browser no longer asks the host for its speakers; the list a
  page sees is unchanged.

## [0.6.0] - 2026-10-13

The pin moves to invisible-core 44.39.0, which seals the firefox-44 engine.

### Changed
- **`waitUntil="networkidle2"` is pyppeteer's**: at most two requests in
  flight for 500 ms. It waited for none, so a page that keeps two requests
  open timed out.
- **Navigation waits are decided as pyppeteer's `NavigatorWatcher`** in
  `goto`, `waitForNavigation`, `reload`, `goBack` and `goForward`:
  - a document that commits in place of the one asked for is waited on in
    its place, and nothing raises; it was `PageError` "is interrupted by
    another navigation";
  - the answer is the response of the document the wait ended on
    (pyppeteer's `mainFrame._navigationURL`); it was always the first
    document's;
  - the expected event is waited for on the child frames too, those that
    started loading: `domcontentloaded` waits for an iframe's document, and
    a lazy iframe out of view, which never starts, is not waited for.
- **Four errors are pyppeteer's**: `page.type` on a selector that matches
  nothing is `PageError` "Cannot find #x on this page"; a key that is not on
  the layout is `PyppeteerError` "Unknown key: X" (it was the engine's
  `ValueError`); handling a request with interception off says "Request
  interception is not enabled."; closing the default context says
  "Non-incognito profile cannot be closed".

### Fixed
- **A cookie that declared no `sameSite` has none in `cookies()`**, as
  Chrome's cookie objects; every cookie carried `sameSite: "None"`.
- **Text with a character that has no key** (an accented letter, an emoji)
  is typed as pyppeteer types it: the keys that exist are pressed and the
  character is inserted in its place. `keyboard.type(text, delay=N)`
  raised, and without a delay the whole text was inserted with no
  keystroke.
- **On Linux, the line endings a page reads are Windows'**: a `Blob` or a
  `File` made with `endings: "native"` turns `\n` into `\r\n`, and the text
  of a selection across two blocks is joined by `\r\n`, as on Windows.
- **`speechSynthesis.getVoices()` is Windows' list**: David, Mark, Zira and
  the two Desktop voices, in that order, and none of them is the default.

### Not yet as the original
- `keyboard.press(key, text=...)` and `down(key, text=...)` ignore `text`:
  the engine turns a key event that carries another text into an input
  method composition (the page sees the key "Process"), which no keyboard
  sends, so the option waits for the engine.
- `browser.disconnect()` closes the browser, where pyppeteer leaves it
  running.
- Where Firefox and pyppeteer's Chromium fire different events, the same
  rule ends in a different place. A page that replaces itself in its head
  (`location.replace`): Chromium fires the first page's `load` before the
  second commits, so pyppeteer answers the first page; Firefox fires none,
  so `goto` answers the second. A script navigation of the page being left
  while a `goto` is on its way: Chromium drops it, Firefox lets it replace
  the `goto`, which then ends on it.
- A `goto` to a `data:` URL emits no `request` event; pyppeteer's does.

## [0.5.0] - 2026-10-13

The pin moves to invisible-core 43.39.0, which seals the firefox-43 engine:
a page's frames no longer drop out of `page.frames` after a few navigations
in one tab, and on Linux AAC is answered as on Windows.

### Changed
- **`select` works as pyppeteer's**: a hidden or disabled `<select>` is
  selected in, unknown values are ignored (`[]` when nothing matches), a value
  that is not a string is `TypeError` ("Values must be string. Found 1 of type
  <class 'int'>"), a selector that matches nothing is `ElementHandleError`
  'Error: failed to find element matching selector "#nope"', and an element
  that is not a `<select>` is pyppeteer's `ElementHandleError`. The options are
  chosen by the engine, so `input` and `change` are trusted events.
- **`waitForSelector` and `waitForXPath` start at the call**, as pyppeteer's
  `WaitTask`: they return a task already running, poll in the page, answer the
  hidden element (or a handle to `true` when it is absent) for `hidden=True`,
  use pyppeteer's idea of visible (any of top, bottom, width or height), say
  " to be hidden" when they time out, and end with "waitForFunction failed:
  frame got detached." when their frame goes.
- **`addScriptTag` and `addStyleTag` wait for a tag by `url` to load** and
  raise "Loading script from X failed" / "Loading style from X failed"; with
  none of `url`, `path` or `content` they raise pyppeteer's `ValueError`.
- **`setViewport` refuses a size that is not a positive integer** with
  pyppeteer's `TypeError` and `ValueError`, and `page.viewport` is the dict
  that was passed.
- **`waitUntil` is validated as pyppeteer validates it**: `commit` is
  refused, `documentloaded` warns and means `domcontentloaded`, `networkidle`
  and the removed options raise pyppeteer's words.
- **`close(runBeforeUnload=True)` lets the page ask** (a `dialog` of type
  `beforeunload`); the page closes when that is accepted. Closing a closed
  page raises "Protocol Error: Connection Closed. Most likely the page has
  been closed."
- **The cookies are the ones a URL would send**, as Chrome's
  `Network.getCookies` answers pyppeteer: host, a path prefix ending at a
  '/', no `secure` cookie to http. `deleteCookie` deletes what matches the url
  or the domain and the path; `setCookie` takes the page's url when none is
  given and refuses `about:blank` and `data:` URLs in pyppeteer's words.

### Fixed
- **A child frame keeps its name and, once detached, its url**; a
  same-document navigation is `framenavigated`.
- A page that closes on its own (`window.close()`, or after its
  `beforeunload` was accepted) no longer leaves its listeners on the
  browser's connection for the life of the browser.

## [0.4.0] - 2026-10-13

The pin moves to invisible-core 41.39.0.

### Changed
- **`click`, `hover` and `tap` act once, as pyppeteer's**: the element is
  scrolled into view if an IntersectionObserver does not see it whole, its
  clickable point is the centre of its first quad larger than one pixel, and
  the pointer acts there. A detached node raises "Node is detached from
  document", a node with no box "Node is either not visible or not an
  HTMLElement", at once; there is no check that the element is enabled,
  stable or the topmost one, and no second attempt. Until 0.3.7 they waited
  for all of that until the timeout.
- `focus` is `element.focus()`, with no check.
- `boundingBox` answers `None` when the engine refuses the question.
- `waitForFunction` raises the predicate's error at once, as pyppeteer's
  `WaitTask` rejects with it, and refuses an unknown `polling` with
  pyppeteer's `ValueError`s.
- `setContent` returns once the document is written and does not wait for
  its `load`.

### Fixed
- **`goBack`, `goForward`, `reload` and `waitForNavigation` end at once on a
  same-document navigation**, with no response, as pyppeteer's
  `NavigatorWatcher`; the history methods waited out their timeout first.
  And a timeout of theirs raises `TimeoutError` ("Navigation Timeout
  Exceeded"), where it answered `None`.

## [0.3.7] - 2026-10-12

The pin moves to invisible-core 41.38.0.

### Fixed
- **The caller's function runs once when its page navigates while it
  waits.** A function passed to `evaluate` or `evaluateHandle` that was
  waiting on a promise when its page navigated was sent again into the page
  that replaced it, so a function with an effect ran twice. It now raises
  `NetworkError` at once, "Execution context was destroyed, most likely
  because of a navigation.", as pyppeteer does, and is never sent again.
  `waitForFunction` keeps polling on the new page.
- **A vanished execution context is a `NetworkError`**, pyppeteer's class for
  it; it was an `ElementHandleError`.

## [0.3.6] - 2026-10-12

The pin moves to invisible-core 41.37.0.

### Fixed
- **No time limit of the engine's own.** Making a context, opening a page,
  closing one and applying a context option each had a fixed limit (30, 20 and
  10 s) that pyppeteer does not have, so a loaded host failed where the original
  waits. They now wait as long as the original does; the limits the API
  declares stay.
- **A browser that dies ends every wait at once.** A wait for an event - a
  page's load state, its main frame, a navigation - had nothing to wake it when
  the browser closed or crashed, so it ran to its timeout. It now raises at
  once that the target closed.

## [0.3.5] - 2026-10-12

The pin moves to invisible-core 41.36.0.

### Fixed
- **A page that replaces itself right after it loads no longer breaks the
  next call.** A read made while the page was moving on failed with the
  engine's raw "Failed to find execution context"; the core now makes such a
  read again on the page that took its place, and says so with its own error
  type when it cannot (an element of the page that is gone).
- **Frames follow the documents.** After a navigation that moved the page to
  another browser process, the old document's iframes stayed listed.
- **A `goto` the browser abandons answers at once.** Sent while the previous
  page's own redirect was in flight, `goto` could lose to it: Firefox loads the
  redirect and drops ours without a word, and the call waited for its whole
  timeout. It now raises a PageError at once, `Navigation to "X" is
  interrupted by another navigation to "Y"`.

## [0.3.4] - 2026-10-11

The pin moves to invisible-core 41.35.0, which seals the firefox-41 engine.

### Fixed
- **On Linux a window covered by another one is not reported as hidden**, the
  same as on Windows: pages in a visible session covered by a second session's
  window read `document.visibilityState === "hidden"` and had their timers and
  animation frames throttled.
- **A launch gets the whole timeout it was given.** Only the wait for the
  browser to start had it; the proxy command and the last startup step after
  it had 10 and 30 seconds of their own. On a loaded machine the proxy command
  could go unanswered for 10 seconds and the launch failed with "the engine
  refused the proxy" while it still had time. One deadline now covers the
  whole startup, and running past it raises a `TimeoutError` instead of
  claiming a refusal. A launch that fails after the browser started now closes
  it and removes the session's files.

## [0.3.3] - 2026-10-11

### Fixed
- **The shutdown pings leave by the session's proxy.** At the end of a session
  longer than a minute, Firefox posts its shutdown telemetry from a separate
  process. That process took its network settings from the machine, so a
  session behind a SOCKS5 or HTTP proxy sent those pings from the machine's own
  address. With the firefox-40 engine (invisible-core 40.35.0) they go through
  the same proxy as the session, or directly when the session has none, and the
  session waits for them to finish before it removes its files: closing a long
  session takes about a second more.

## [0.3.2] - 2026-10-10

### Fixed
- **A session no longer leaves its temporary profile behind.** On Windows a
  session longer than a minute left an `invisible_profile_*` directory in
  `%TEMP%`, holding an empty `saved-telemetry-pings`: the profile was removed
  right after the browser, while the `pingsender.exe` it starts at exit still
  held a file in it. The session's directories (the core's `SessionFiles`)
  are now removed after every process of the session has ended, and a new
  session sweeps what a killed one left.
- **The browser's temporary files go with the session.** Firefox wrote into
  the system temporary directory, and a short session cut off its cleanup: a
  70 s session left a 4 MB certificate bundle in `%TEMP%`. The browser now
  gets a temporary directory of its own, removed with the profile.
- **Characters that need Shift are typed with Shift.** `@`, `!` and capital
  letters reached the page with `shiftKey` false and no Shift keydown, a
  combination no keyboard produces; with Shift held by hand, pressing `2` wrote
  `2`. They are typed with Shift down now, timed by the session's hand
  (invisible-core 39.35.0).
- **A pause before every click.** The press came one protocol round trip after
  the pointer arrived, where a hand waits a moment. The session's hand now
  pauses at the end of the approach, before the button goes down.

## [0.3.1] - 2026-10-10

### Fixed
- **Pointer actions answer once the page has handled the event.** With the
  firefox-39 engine, a press, a move or a release returns after the page's
  listeners have run. Before, the next call could reach the page first: under
  load a short drag-and-drop lost its drop, and a script read right after a
  double click could count one click short.

### Changed
- **A click that opens `alert()` is handled by the engine.** The engine ends
  its own wait when a dialog opens, so this package no longer carries a hook
  for it.

## [0.3.0] - 2026-10-09

### Changed
- **The Juggler client comes from `invisible-core`** (38.34.0,
  `invisible_core.juggler`), the one copy every invisible_ wrapper shares.
  This package carried its own, taken from invisible-playwright on
  2026-09-24, and it now has the fixes made there since:
  - typing into a field waits the moment a person takes after focusing it,
    and longer while the field is still changing, so a page that writes into
    the field when it gets the focus no longer overwrites what is typed;
  - a picked file reaches the input after the hesitation a person takes to
    choose it;
  - an action that needs no point on the screen (setting the files of a
    hidden `<input type=file>` behind a styled button, focusing or
    dispatching an event on a hidden element) no longer waits for the element
    to become visible until it times out;
  - every page of a session numbers its own clicks, fields and typed strings,
    so two tabs never draw the same movement.
- **A preference value that is not a bool, an int, a float or a string is
  refused** with a `TypeError` naming it, instead of being written as its
  text.
- **The license is MIT only.** The two files taken from Playwright's driver
  (Apache-2.0) moved to invisible-core with the client.

## [0.2.2] - 2026-10-09

### Changed
- **The window a page reads is the one Windows Firefox draws.** Through
  `invisible-core` 38.33.0 and the firefox-38 engine. A maximized window
  answers `screenX` -8 and an `outerWidth` 16 wider than `screen.availWidth`
  at 100%, as Windows Firefox does, where it answered 0 and the work area; its
  content starts where retail's does at every display scale; a popup answers
  its own size, frame and position, and one opened without a position is
  placed where Windows Firefox places it.

### Removed
- **The pins `screen.chrome_w`, `screen.chrome_h`, `screen.window_x` and
  `screen.window_y`.** The window follows `screen.dpr`, which takes 1, 1.25,
  1.5 or 2.

## [0.2.1] - 2026-10-09

### Changed
- **The screen a page reads is in CSS pixels.** Through `invisible-core`
  37.33.0 and the firefox-37 engine. A 1920x1080 panel at 125% reports
  `screen.width` 1536 and `screen.height` 864, as Firefox on that monitor
  does. Before, it reported the panel's device pixels. The default
  viewport is derived the same way.

### Fixed
- **On Linux the virtual display keeps X access control on.** The Xvfb
  display opened for a headed session without a screen used to run with
  access control off (`-ac`), so any local process could connect to it and
  read or drive the browser window. It now gets a private session cookie,
  handed only to the browser, and removed when the session stops.

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
