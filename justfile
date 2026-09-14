set shell := ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

# Optional legacy export; the normal workflow uses the persistent Chrome profile.
auth_state := "pacepilot-auth.json"

# Session helper script that powers all recipes.
session_script := justfile_directory() + "/scripts/agent_browser_session.ps1"

# List all browser session recipes
default:
    @just --list

# Recommended: open dedicated Chrome and wait for a real PacePilot login
agent-browser port="9222":
    @& "{{session_script}}" -Action get -Port {{port}}

# Open real Chrome with a dedicated automation profile and the CDP debugging port
open-browser port="9222" app_url="http://localhost:8000/":
    @& "{{session_script}}" -Action browser -Port {{port}} -AppUrl "{{app_url}}"

# Wait until a protected PacePilot page is accessible in the same Chrome profile
wait-login port="9222" timeout="600" app_url="http://localhost:8000/":
    @& "{{session_script}}" -Action wait -Port {{port}} -TimeoutSeconds {{timeout}} -AppUrl "{{app_url}}"

# Optional legacy auth-state export (not needed for CDP)
save-state port="9222":
    @& "{{session_script}}" -Action save -Port {{port}} -StateFile "{{auth_state}}"

# Compatibility command: now uses the same CDP workflow, without exporting cookies
get-session port="9222" app_url="http://localhost:8000/":
    @& "{{session_script}}" -Action get -Port {{port}} -AppUrl "{{app_url}}"

# Report whether the automation browser is running and signed in
check port="9222":
    @& "{{session_script}}" -Action check -Port {{port}}
