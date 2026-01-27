# Simple Update Workflow

The easiest way to update ChiefWiggum:

## Development Install (Editable)

```bash
# Update everything
wig update
```

That's it! This command:
1. Runs `git pull` to get latest code
2. Reinstalls the package to pick up changes
3. Verifies the installation

## Production Install (pipx)

```bash
# Update via pipx
wig update
```

Or manually:
```bash
pipx upgrade chiefwiggum
```

## Production Install (PyPI)

```bash
# Update from PyPI
wig update
```

Or manually:
```bash
pip install --upgrade chiefwiggum
```

## Check for Updates Without Installing

```bash
wig update --check
```

## Old Workflow (Still Works)

If you prefer the manual approach:

```bash
# Development
git pull
make reinstall
make verify

# pipx
pipx upgrade chiefwiggum

# PyPI
pip install --upgrade chiefwiggum
```

## Benefits of `wig update`

✅ **Smart Detection** - Automatically detects how ChiefWiggum is installed
✅ **One Command** - Does everything needed to update
✅ **Safe** - Handles errors gracefully with helpful messages
✅ **Fast** - Optimized for quick updates
✅ **Verifies** - Shows new version after update

## Troubleshooting

If `wig update` fails:

1. **Check your installation type:**
   ```bash
   which chiefwiggum
   ```

2. **Verify you're in the right directory (for dev installs):**
   ```bash
   pwd  # Should be in chiefwiggum repo
   ```

3. **Check git status (for dev installs):**
   ```bash
   git status
   ```

4. **Use the manual method:**
   ```bash
   make reinstall  # Development
   pipx upgrade chiefwiggum  # pipx
   ```

## Examples

```bash
# Check if updates are available
wig update --check

# Update to latest
wig update

# Verify it worked
wig verify
```
