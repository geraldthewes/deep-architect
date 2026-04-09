# Playwright CLI Reference

Complete reference for `playwright-cli` commands used in browser automation.

## Installation

```bash
npm install -g @playwright/cli@latest
playwright-cli --help
```

## Sessions

Playwright CLI uses persistent browser profiles by default. Use named sessions to manage multiple independent browser instances.

```bash
# Named sessions
playwright-cli -s=<name> <command>        # Run command in named session
playwright-cli list                       # List all active sessions
playwright-cli close-all                  # Close all browser instances
playwright-cli kill-all                   # Force kill all browser processes
playwright-cli -s=<name> close            # Close specific session
playwright-cli -s=<name> delete-data      # Delete session profile data
```

**Environment variable:**
```bash
PLAYWRIGHT_CLI_SESSION=my-session claude .
```

## Core Commands

```bash
playwright-cli open [url]                 # Open browser, optionally navigate to URL
playwright-cli goto <url>                 # Navigate to URL
playwright-cli close                      # Close the page
playwright-cli type <text>                # Type text into focused element
playwright-cli click <ref> [button]       # Click on element (ref from snapshot)
playwright-cli dblclick <ref> [button]    # Double-click on element
playwright-cli fill <ref> <text>          # Fill text into input field
playwright-cli drag <startRef> <endRef>   # Drag and drop between elements
playwright-cli hover <ref>                # Hover over element
playwright-cli select <ref> <value>       # Select option in dropdown
playwright-cli upload <file>              # Upload file(s)
playwright-cli check <ref>                # Check checkbox/radio
playwright-cli uncheck <ref>              # Uncheck checkbox
playwright-cli snapshot                   # Capture page snapshot (get element refs)
playwright-cli snapshot --filename=f      # Save snapshot to file
playwright-cli eval <func> [ref]          # Evaluate JavaScript on page or element
playwright-cli dialog-accept [prompt]     # Accept dialog
playwright-cli dialog-dismiss             # Dismiss dialog
playwright-cli resize <w> <h>             # Resize browser window
```

## Navigation

```bash
playwright-cli go-back                    # Go to previous page
playwright-cli go-forward                 # Go to next page
playwright-cli reload                     # Reload current page
```

## Keyboard

```bash
playwright-cli press <key>                # Press key (a, Enter, ArrowLeft, etc.)
playwright-cli keydown <key>              # Press key down
playwright-cli keyup <key>                # Release key
```

## Mouse

```bash
playwright-cli mousemove <x> <y>          # Move mouse to position
playwright-cli mousedown [button]         # Press mouse button down
playwright-cli mouseup [button]           # Release mouse button
playwright-cli mousewheel <dx> <dy>       # Scroll mouse wheel
```

## Screenshots & PDF

```bash
playwright-cli screenshot [ref]           # Screenshot page or element
playwright-cli screenshot --filename=f    # Save screenshot with specific name
playwright-cli pdf                        # Save page as PDF
playwright-cli pdf --filename=page.pdf    # Save PDF with specific name
```

## Tabs

```bash
playwright-cli tab-list                   # List all open tabs
playwright-cli tab-new [url]              # Create new tab
playwright-cli tab-close [index]          # Close tab by index
playwright-cli tab-select <index>         # Switch to tab by index
```

## Storage

### State Management
```bash
playwright-cli state-save [filename]      # Save browser state (cookies, storage)
playwright-cli state-load <filename>      # Load browser state
```

### Cookies
```bash
playwright-cli cookie-list [--domain]     # List all cookies
playwright-cli cookie-get <name>          # Get cookie value
playwright-cli cookie-set <name> <value>  # Set cookie
playwright-cli cookie-delete <name>       # Delete cookie
playwright-cli cookie-clear               # Clear all cookies
```

### LocalStorage
```bash
playwright-cli localstorage-list          # List localStorage entries
playwright-cli localstorage-get <key>     # Get value
playwright-cli localstorage-set <k> <v>   # Set value
playwright-cli localstorage-delete <key>  # Delete entry
playwright-cli localstorage-clear         # Clear all
```

### SessionStorage
```bash
playwright-cli sessionstorage-list        # List sessionStorage entries
playwright-cli sessionstorage-get <key>   # Get value
playwright-cli sessionstorage-set <k> <v> # Set value
playwright-cli sessionstorage-delete <k>  # Delete entry
playwright-cli sessionstorage-clear       # Clear all
```

## Network

```bash
playwright-cli route <pattern> [opts]     # Mock network requests
playwright-cli route-list                 # List active routes
playwright-cli unroute [pattern]          # Remove routes
playwright-cli network                    # List network requests
```

## DevTools

```bash
playwright-cli console [min-level]        # List console messages
playwright-cli run-code <code>            # Execute Playwright code snippet
playwright-cli tracing-start              # Start trace recording
playwright-cli tracing-stop               # Stop trace recording
playwright-cli video-start                # Start video recording
playwright-cli video-stop [filename]      # Stop video recording
```

## Configuration

### Command-line Options
```bash
playwright-cli open --headed              # Show browser window
playwright-cli open --browser=chrome      # Use specific browser (chrome/firefox/webkit)
playwright-cli open --persistent          # Use persistent profile
playwright-cli open --profile=<path>      # Use custom profile directory
playwright-cli open --config=file.json    # Use config file
playwright-cli open --extension           # Connect via browser extension
```

### Environment Variables

Common environment variables:

```bash
PLAYWRIGHT_CLI_SESSION=<name>             # Default session name
PLAYWRIGHT_MCP_VIEWPORT_SIZE=1440x900     # Set viewport size
PLAYWRIGHT_MCP_CAPS=vision                # Enable vision mode (screenshots in context)
PLAYWRIGHT_MCP_BROWSER=chromium           # Browser type (chromium/firefox/webkit)
PLAYWRIGHT_MCP_HEADLESS=true              # Run headless
PLAYWRIGHT_MCP_OUTPUT_DIR=./screenshots   # Output directory for files
```

Full list of environment variables: see official Playwright CLI documentation.

### Configuration File

Create `playwright-cli.json` in your working directory:

```json
{
  "browser": {
    "browserName": "chromium",
    "launchOptions": { "headless": true },
    "contextOptions": {
      "viewport": { "width": 1440, "height": 900 }
    }
  },
  "outputDir": "./screenshots",
  "outputMode": "stdout",
  "console": {
    "level": "info"
  },
  "timeouts": {
    "action": 5000,
    "navigation": 60000
  }
}
```

Or specify config file path:
```bash
playwright-cli --config path/to/config.json open example.com
```

## Installation & Setup

```bash
# Install globally
npm install -g @playwright/cli@latest

# Install skills for Claude Code
playwright-cli install --skills

# Install browser
playwright-cli install-browser
```

## Examples

### Basic workflow
```bash
# Open browser with session
PLAYWRIGHT_MCP_VIEWPORT_SIZE=1440x900 playwright-cli -s=demo open https://example.com --persistent --headed

# Get element references
playwright-cli -s=demo snapshot

# Interact with page
playwright-cli -s=demo click e12
playwright-cli -s=demo fill e34 "search query"
playwright-cli -s=demo press Enter

# Capture result
playwright-cli -s=demo screenshot --filename=result.png

# Close session
playwright-cli -s=demo close
```

### Parallel sessions
```bash
# Open multiple independent sessions
playwright-cli -s=session-1 open https://site1.com --persistent
playwright-cli -s=session-2 open https://site2.com --persistent

# Work with them independently
playwright-cli -s=session-1 snapshot
playwright-cli -s=session-2 snapshot

# Close all
playwright-cli close-all
```

### Vision mode (screenshots as context)
```bash
PLAYWRIGHT_MCP_VIEWPORT_SIZE=1440x900 PLAYWRIGHT_MCP_CAPS=vision playwright-cli -s=visual open https://example.com --persistent
playwright-cli -s=visual snapshot  # Screenshot returned as image in context
```

## Best Practices

1. **Always use named sessions** - Derive meaningful session names from the task
2. **Always close sessions** - Call `playwright-cli -s=<name> close` when done
3. **Set viewport size** - Use `PLAYWRIGHT_MCP_VIEWPORT_SIZE` for consistency
4. **Use persistent profiles** - Add `--persistent` to preserve cookies/state
5. **Snapshot before interact** - Get fresh element references with `snapshot`
6. **Vision mode selectively** - Only use when screenshots need to be in context (higher token cost)

## Troubleshooting

```bash
# List running sessions
playwright-cli list

# Force close everything
playwright-cli kill-all

# Delete session data
playwright-cli -s=<name> delete-data

# Check console for errors
playwright-cli -s=<name> console error
```

## Related Documentation

- Official Playwright CLI: https://github.com/microsoft/playwright-cli
- Playwright Documentation: https://playwright.dev
