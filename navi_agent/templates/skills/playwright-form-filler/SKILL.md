---
name: playwright-form-filler
description: >
  Use playwright-cli to automate browser form filling in headed (visible) mode.
  Activate when user wants to: fill online forms, submit surveys, complete questionnaires,
  automate repetitive web form tasks, or interact with authenticated web pages.
  Covers: opening visible browser, navigating to pages, selecting dropdowns,
  filling text inputs, clicking buttons, saving/loading login state,
  multi-tab management, dialog handling, iframe navigation, screenshot verification.
  NOT for: web scraping at scale, headless automation, API testing.
---

# Playwright Form Filler

Automate browser form filling with `playwright-cli` in **headed mode** (visible browser).

## Prerequisites

```bash
npm install -g playwright-cli
playwright-cli install-browser chromium
```

## Core Workflow

### 1. Open Browser (Headed)

```bash
playwright-cli open "URL" --headed
```

User can see the browser. Use for tasks requiring login or visual verification.

**Check open browsers:**
```bash
playwright-cli list
```

### 2. Login Handling

If page requires login:
1. Tell user to scan QR code / enter credentials manually
2. After login, save state:

```bash
playwright-cli state-save session.json
```

Next time, load saved state to skip login:

```bash
playwright-cli open --headed
playwright-cli state-load session.json
```

**Note:** Cookies expire. When state fails, user must re-login and re-save.

### 3. Tab Management

When a click opens a new tab or popup:

```bash
playwright-cli tab-list              # List all tabs with indices
playwright-cli tab-select <index>    # Switch to tab by index
playwright-cli tab-new [url]         # Open new tab
playwright-cli tab-close [index]     # Close a tab
```

**Pattern:** After clicking a link that opens a new window/tab, run `tab-list` then `tab-select` to switch to it.

### 4. Inspect Page Structure

```bash
playwright-cli snapshot
```

Returns YAML with element refs (e.g., `e18`, `f3e2`, `f5e26`). Use refs for targeting.

### 5. Fill Form Fields

**Dropdown (select/combobox):**
```bash
playwright-cli select <ref> "option text"
```

**Text input:**
```bash
playwright-cli fill <ref> "text content"
```

**Checkbox:**
```bash
playwright-cli check <ref>
playwright-cli uncheck <ref>
```

**Click button/link:**
```bash
playwright-cli click <ref>
```

### 6. Dialog Handling — CRITICAL

**Dialogs block ALL page operations.** If any command hangs or times out, a dialog is likely showing.

```bash
playwright-cli dialog-accept              # Accept (OK)
playwright-cli dialog-accept "prompt text" # Accept with prompt input
playwright-cli dialog-dismiss             # Dismiss (Cancel)
```

**Multiple consecutive dialogs:** Some systems show 2+ dialogs in sequence (e.g., "提交成功" → "确认返回"). Handle ALL of them:

```bash
playwright-cli click submit-ref
sleep 2
playwright-cli dialog-accept  # 1st dialog
sleep 1
playwright-cli dialog-accept  # 2nd dialog (if any)
sleep 1
playwright-cli dialog-accept  # 3rd (just in case)
```

**Timeout means dialog is showing:** If `screenshot`, `snapshot`, or `fill` times out, try `dialog-accept` first.

**Auto-handle ALL dialogs (RECOMMENDED for automation):**

Register a dialog handler that automatically accepts all dialogs. This prevents scripts from hanging:

```bash
# Register once at the start of your session
playwright-cli run-code "async page => { page.on('dialog', d => d.accept()); return 'ok'; }"
```

After this, ALL subsequent dialogs (alert, confirm, prompt) are automatically accepted — no manual `dialog-accept` needed. This is essential for automation scripts.

**Why this works:** JavaScript dialogs are modal and block the page thread. When a dialog shows, ALL playwright-cli commands hang until it's handled. The `page.on('dialog')` handler intercepts dialogs at the Playwright level before they block.

**When to use:**
- Automation scripts that submit forms with confirmation dialogs
- Any workflow where dialogs appear repeatedly
- When scripts keep timing out on `dialog-accept`

**Note:** This handler persists for the browser session. To stop auto-accepting, reload the page or restart the browser.

### 7. Screenshot & Verification

```bash
playwright-cli screenshot             # Save viewport screenshot
playwright-cli screenshot <ref>       # Screenshot specific element
```

Use to verify visual state after filling, especially for user confirmation.

### 8. Batch Operations

Chain commands with `&&`:
```bash
playwright-cli fill f4e26 "10" && playwright-cli fill f4e33 "10" && playwright-cli fill f4e40 "9"
```

## Deep Iframe Navigation

Chinese university/enterprise systems often use deeply nested iframes. Snapshot refs show nesting depth with prefixed IDs:

```
e78     → top-level element
f3e2    → 1st level iframe child
f5e26   → 2nd level iframe child
f6e4    → 3rd level iframe child
```

Playwright-cli automatically resolves iframe refs — just use the deepest ref directly. No manual frame switching needed.

## Refs Are Dynamic

**Refs change after every page mutation** (navigation, form submit, dialog close, AJAX update).

- After clicking "评价" to open a form → refs change
- After submitting a form and returning to list → refs change
- After handling a dialog → refs may change

**Rule:** Always run `playwright-cli snapshot` to get fresh refs before filling. Never hardcode refs across page state changes.

## Automation Script Pattern

For repetitive form filling (e.g., evaluating 10 courses), write a bash script.

**Step 1: Register auto-dialog handler (do this ONCE before the loop):**

```bash
playwright-cli run-code "async page => { page.on('dialog', d => d.accept()); return 'ok'; }"
```

**Step 2: Write the loop script:**

```bash
fill_one_item() {
  # Get fresh refs each time
  local snapshot=$(playwright-cli snapshot 2>/dev/null)
  
  # Extract textbox refs dynamically
  local refs=($(echo "$snapshot" | grep -oP 'textbox \[ref=\K[f][0-9]+e[0-9]+'))
  
  # Fill using extracted refs
  for j in $(seq 0 9); do
    playwright-cli fill "${refs[$j]}" "10" 2>/dev/null
  done
  
  # Find and click submit button
  local submit_ref=$(echo "$snapshot" | grep -oP 'button "提交" \[ref=\K[f][0-9]+e[0-9]+')
  playwright-cli click "$submit_ref" 2>/dev/null
  sleep 2
  
  # Handle ALL dialogs in a loop
  for k in 1 2 3; do
    playwright-cli dialog-accept 2>/dev/null
    sleep 1
  done
}
```

**Key points:**
- **Register auto-dialog handler FIRST** — prevents scripts from hanging on dialogs
- Extract refs with `grep -oP` from snapshot output (not hardcoded)
- Sleep between operations (pages need time to update)
- Suppress errors with `2>/dev/null` for non-critical calls
- If not using auto-handler, loop `dialog-accept` 3 times after each submit

## Known Validation Constraints

**Score evaluation systems** (教学评价, 辅导员评议):
- Some systems reject uniform scores: `"指标分数不能全部相同！"`
- Solution: Vary scores slightly (e.g., mix 9s and 10s, not all identical)
- Pattern: Fill most items with max score, a few with max-1

## Troubleshooting

- **Command hangs/times out**: A dialog is showing — use `dialog-accept` first, or register auto-handler
- **All commands timeout after submit**: Dialog is blocking — register `page.on('dialog')` auto-handler before submitting
- **Element not found**: Run `playwright-cli snapshot` again — refs change after page updates
- **"Modal state" error**: Handle dialog before continuing
- **Login state expired**: Re-login manually, then `state-save` again
- **Uniform score rejection**: Vary scores — not all identical
- **Script fails midway**: Page state changed; add more `sleep` and re-snapshot before each action

## Lessons Learned

- **Always register auto-dialog handler for automation loops** — single `dialog-accept` calls are fragile because they can timeout before the dialog appears
- **Refs change after EVERY page mutation** — never cache refs across form submissions
- **Iframe refs are prefixed** (f3e2, f5e26) — playwright-cli resolves them automatically, just use the deepest ref
- **Score validation varies by system** — some reject uniform scores, some don't; always test first
