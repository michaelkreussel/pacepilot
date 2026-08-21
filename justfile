set shell := ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

# Agent-browser auth state file created by `just get-session`.
auth_state := "pacepilot-auth.json"

# Session helper script that powers all recipes.
session_script := justfile_directory() + "/scripts/agent_browser_session.ps1"

# List all browser session recipes
default:
    @just --list

# Open real Chrome with a dedicated automation profile and the CDP debugging port
open-browser port="9222":
    @& "{{session_script}}" -Action browser -Port {{port}}

# Wait until the Google sign-in completes and the PacePilot session cookie is set
wait-login port="9222" timeout="600":
    @& "{{session_script}}" -Action wait -Port {{port}} -TimeoutSeconds {{timeout}}

# Save the signed-in browser auth state for agent-browser to reuse
save-state port="9222":
    @& "{{session_script}}" -Action save -Port {{port}} -StateFile "{{auth_state}}"

# One-shot: open Chrome, wait for sign-in, then save the auth state
get-session port="9222":
    @& "{{session_script}}" -Action get -Port {{port}} -StateFile "{{auth_state}}"

# Report whether the automation browser is running and signed in
check port="9222":
    @& "{{session_script}}" -Action check -Port {{port}}
