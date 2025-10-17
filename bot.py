# bot.py — Discord Dashboard Bot for Revolt Monitor Control

import os
import json
import subprocess
import signal
import requests
import psutil
import time
from discord.ext import commands
from discord import Intents, app_commands, Interaction, Embed, ButtonStyle, SelectOption, Activity, ActivityType
from discord.ui import View, Button, Select, Modal, TextInput
import discord

# Directory containing config_*.json and launch_*.sh
CONFIG_DIR = os.getcwd()
PID_DIR = os.path.join(CONFIG_DIR, "pids")
STATE_DIR = os.path.join(CONFIG_DIR, "states")

# Admin user ID with full access to all configs
ADMIN_USER_ID = 772336857970114590

# Ensure directories exist
os.makedirs(PID_DIR, exist_ok=True)
os.makedirs(STATE_DIR, exist_ok=True)

bot = commands.Bot(command_prefix="/", intents=Intents.default())

# Utilities

def list_users():
    return [f.replace("config_", "").replace(".json", "")
            for f in os.listdir(CONFIG_DIR)
            if f.startswith("config_") and f.endswith(".json")]

def config_path(user):
    return os.path.join(CONFIG_DIR, f"config_{user}.json")

def launch_script(user):
    return os.path.join(CONFIG_DIR, f"launch_{user}.sh")

def pid_file(user):
    return os.path.join(PID_DIR, f"{user}.pid")

def state_file(user):
    return os.path.join(STATE_DIR, f"{user}_state.json")

def find_user_by_owner_id(owner_id):
    """Find user config by Discord owner ID"""
    for user in list_users():
        config = load_config(user)
        if config.get("ownerId") == owner_id:
            return user
    return None

def is_admin(user_id):
    """Check if user is admin"""
    return user_id == ADMIN_USER_ID

def can_manage_config(user_id, config):
    """Check if user can manage a config"""
    return is_admin(user_id) or config.get("ownerId") == user_id

def load_config(user):
    """Load full config with backwards compatibility"""
    try:
        with open(config_path(user)) as f:
            data = json.load(f)
        
        # Handle backwards compatibility - if it's just an array, convert it
        if isinstance(data, list):
            # Generate default ports based on user for backwards compatibility
            base = abs(hash(user)) % 1000
            return {
                "ownerId": None,  # Will need to be set manually
                "ports": {
                    "chrome": 9222 + base,
                    "ws": 5678 + base,
                    "tcp": 5679 + base
                },
                "tempDir": f"/tmp/revolt_{user}",
                "servers": data
            }
        
        # Already in new format - ensure ownerId exists
        if "ownerId" not in data:
            data["ownerId"] = None
        
        return data
    except FileNotFoundError:
        # Generate default config for new users
        base = abs(hash(user)) % 1000
        return {
            "ownerId": None,
            "ports": {
                "chrome": 9222 + base,
                "ws": 5678 + base,
                "tcp": 5679 + base
            },
            "tempDir": f"/tmp/revolt_{user}",
            "servers": []
        }

def save_config(user, config_data):
    """Save full config structure"""
    with open(config_path(user), 'w') as f:
        json.dump(config_data, f, indent=2)

def save_state(user, state):
    """Save current state locally"""
    try:
        state_data = {"status": state, "timestamp": time.time()}
        with open(state_file(user), 'w') as f:
            json.dump(state_data, f, indent=2)
        print(f"[DEBUG] Saved state for {user}: {state}")
    except Exception as e:
        print(f"Failed to save state for {user}: {e}")

def load_state(user):
    """Load saved state with better error handling"""
    try:
        state_path = state_file(user)
        if not os.path.exists(state_path):
            return "unknown"
        
        # Check if file is empty or corrupted
        if os.path.getsize(state_path) == 0:
            print(f"[DEBUG] Empty state file for {user}")
            return "unknown"
        
        with open(state_path, 'r') as f:
            content = f.read().strip()
            if not content:
                print(f"[DEBUG] Empty state file content for {user}")
                return "unknown"
            
            data = json.loads(content)
            status = data.get("status", "unknown")
            print(f"[DEBUG] Loaded state for {user}: {status}")
            return status
            
    except json.JSONDecodeError as e:
        print(f"[DEBUG] JSON decode error for {user}: {e}")
        # Try to recreate the file
        try:
            os.remove(state_file(user))
        except:
            pass
        return "unknown"
    except FileNotFoundError:
        print(f"[DEBUG] No state file for {user}")
        return "unknown"
    except Exception as e:
        print(f"Failed to load state for {user}: {e}")
        return "unknown"

def make_launch_cmd(user):
    """Create launch command using ports from config"""
    config = load_config(user)
    ports = config.get("ports", {})
    
    chrome = ports.get("chrome", 9222)
    ws = ports.get("ws", 5678)
    tcp = ports.get("tcp", 5679)
    temp = config.get("tempDir", f"/tmp/revolt_{user}")
    
    return ["bash", launch_script(user), str(chrome), str(ws), str(tcp), temp, config_path(user)]

def is_process_running(user):
    """Check if process exists (regardless of paused/active state)"""
    try:
        with open(pid_file(user), 'r') as f:
            pid = int(f.read().strip())
        return psutil.pid_exists(pid)
    except (FileNotFoundError, ValueError):
        return False

def get_process_status(user):
    """Get detailed process status with improved detection"""
    if not is_process_running(user):
        save_state(user, "stopped")
        return 'stopped'
    
    # Try to get status from control API
    try:
        config = load_config(user)
        chrome_port = config.get("ports", {}).get("chrome", 9222)
        control_port = chrome_port + 1
        
        print(f"[DEBUG] Checking control API for {user} on port {control_port}")
        r = requests.get(f"http://localhost:{control_port}/status", timeout=1)
        print(f"[DEBUG] Control API response: {r.status_code}")
        
        if r.status_code == 200:
            # Try to parse the response
            try:
                status_data = r.json()
                print(f"[DEBUG] Status data: {status_data}")
                if isinstance(status_data, dict):
                    status = (status_data.get("status") or 
                             status_data.get("state") or 
                             status_data.get("running"))
                    if status:
                        actual_status = status.lower()
                        save_state(user, actual_status)
                        return actual_status
                elif isinstance(status_data, str):
                    actual_status = status_data.lower()
                    save_state(user, actual_status)
                    return actual_status
            except json.JSONDecodeError:
                # If not JSON, try plain text
                text = r.text.lower()
                print(f"[DEBUG] Plain text response: {text}")
                if "paused" in text:
                    save_state(user, "paused")
                    return "paused"
                elif "active" in text or "running" in text:
                    save_state(user, "active")
                    return "active"
    
    except requests.exceptions.ConnectionError:
        print(f"[DEBUG] Control API connection failed for {user}")
        # Control API not responding - use last known state
        last_state = load_state(user)
        if last_state in ["paused", "active"]:
            return last_state
        # If no saved state and process exists, assume it's starting up
        return "unknown"
    except Exception as e:
        print(f"Error checking status for {user}: {e}")
    
    # Fallback: if process exists but we can't determine state
    last_state = load_state(user)
    if last_state in ["paused", "active"]:
        return last_state
    
    # Final fallback: assume active if process is running
    save_state(user, "active")
    return "active"

def get_status_display(status):
    """Convert status to display format with emoji"""
    status_map = {
        'active': '🟢 Running',
        'paused': '🟡 Paused', 
        'stopped': '🔴 Stopped',
        'unknown': '🟠 Unknown'
    }
    return status_map.get(status, '❓ Unknown')

def get_status_color(status):
    """Get Discord embed color based on status"""
    color_map = {
        'active': 0x00FF00,    # Green
        'paused': 0xFFFF00,    # Yellow
        'stopped': 0xFF0000,   # Red
        'unknown': 0xFFA500    # Orange
    }
    return color_map.get(status, 0x808080)  # Gray for unknown

def start_process(user):
    current_status = get_process_status(user)
    print(f"[DEBUG] Starting process for {user}, current status: {current_status}")
    
    if current_status == 'active':
        return False, "Already running"
    elif current_status == 'paused':
        # Resume paused process
        try:
            config = load_config(user)
            chrome_port = config.get("ports", {}).get("chrome", 9222)
            control_port = chrome_port + 1
            r = requests.post(f"http://localhost:{control_port}/resume", timeout=2)
            if r.status_code == 200:
                save_state(user, "active")
                return True, "Resumed via control API"
            else:
                return False, f"Failed to resume: {r.status_code}"
        except Exception as e:
            return False, f"Resume error: {str(e)}"
    else:
        # Start new process (status is 'stopped' or 'unknown')
        try:
            cmd = make_launch_cmd(user)
            process = subprocess.Popen(cmd)
            with open(pid_file(user), 'w') as f:
                f.write(str(process.pid))
            save_state(user, "active")
            return True, f"Started with PID {process.pid}"
        except Exception as e:
            return False, f"Failed to start: {str(e)}"

def stop_process(user):
    current_status = get_process_status(user)
    print(f"[DEBUG] Stopping process for {user}, current status: {current_status}")
    
    if current_status == 'stopped':
        return False, "Already stopped"
    elif current_status in ['active', 'paused', 'unknown']:
        # Pause running process
        try:
            config = load_config(user)
            chrome_port = config.get("ports", {}).get("chrome", 9222)
            control_port = chrome_port + 1
            r = requests.post(f"http://localhost:{control_port}/pause", timeout=2)
            if r.status_code == 200:
                save_state(user, "paused")
                return True, "Paused via control API"
            else:
                return False, f"Failed to pause: {r.status_code}"
        except Exception as e:
            return False, f"Pause error: {str(e)}"
    else:
        return False, "Cannot stop - unknown state"

def kill_process(user):
    """Forcefully terminate process (for emergency stop)"""
    try:
        if is_process_running(user):
            with open(pid_file(user), 'r') as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGTERM)
            os.remove(pid_file(user))
            save_state(user, "stopped")
            return True, f"Forcefully terminated PID {pid}"
        else:
            save_state(user, "stopped")
            return False, "Process not running"
    except Exception as e:
        return False, f"Failed to kill: {str(e)}"

# Dashboard command
@bot.tree.command(name="dashboard", description="Open dashboard")
async def dashboard(interaction: Interaction):
    author_id = interaction.user.id
    
    # Check if user is admin - show full dashboard
    if is_admin(author_id):
        await show_admin_dashboard(interaction)
        return
    
    # Regular user - find their config
    user = find_user_by_owner_id(author_id)
    
    if user:
        # User has a config, take them straight to their panel
        await open_user_panel(interaction, user)
    else:
        # No config found for this user
        embed = Embed(
            title="❌ No Configuration Found",
            description=f"No configuration found for your Discord ID: `{author_id}`",
            color=0xFF0000
        )
        
        embed.add_field(
            name="🔧 Setup Required",
            value="Please contact an administrator to set up your configuration with your Discord ID.",
            inline=False
        )
        
        embed.add_field(
            name="📋 Your Discord ID",
            value=f"`{author_id}`",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

# Admin dashboard (original overview)
async def show_admin_dashboard(interaction: Interaction):
    users = list_users()
    embed = Embed(title="🛠️ Revolt Monitor Dashboard (Admin)", color=0x00BFFF)
    
    if not users:
        embed.description = "No user configs found."
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    # Build comprehensive overview
    lines = []
    total_servers = 0
    status_counts = {'active': 0, 'paused': 0, 'stopped': 0, 'unknown': 0}
    
    for user in users:
        config = load_config(user)
        servers = config.get('servers', [])
        status = get_process_status(user)
        status_display = get_status_display(status)
        
        status_counts[status] += 1
        total_servers += len(servers)
        
        # Add owner info to line
        owner_id = config.get('ownerId')
        owner_display = f"<@{owner_id}>" if owner_id else "No owner"
        lines.append(f"**{user}**: {len(servers)} servers — {status_display} — {owner_display}")
    
    # Overview with detailed status counts
    overview_text = f"**Users**: {len(users)}\n**Total Servers**: {total_servers}\n"
    overview_text += f"**Active**: {status_counts['active']} | **Paused**: {status_counts['paused']} | **Stopped**: {status_counts['stopped']}"
    if status_counts['unknown'] > 0:
        overview_text += f" | **Unknown**: {status_counts['unknown']}"
    
    embed.add_field(
        name="📊 Overview",
        value=overview_text,
        inline=False
    )
    
    embed.add_field(
        name="📋 User Status",
        value="\n".join(lines) if lines else "No users configured",
        inline=False
    )

    # View to select user
    class UserSelect(Select):
        def __init__(self):
            opts = []
            for u in users:
                status = get_process_status(u)
                status_display = get_status_display(status)
                opts.append(SelectOption(label=u, description=status_display))
            super().__init__(placeholder="Select user to manage…", options=opts)
        
        async def callback(self, select_inter: Interaction):
            await open_user_panel(select_inter, self.values[0])

    view = View(timeout=300)
    view.add_item(UserSelect())
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# Open per-user panel
async def open_user_panel(interaction: Interaction, user: str):
    config = load_config(user)
    servers = config.get('servers', [])
    status = get_process_status(user)
    status_display = get_status_display(status)
    
    # Check if user can manage this config
    user_can_manage = can_manage_config(interaction.user.id, config)
    
    embed = Embed(
        title=f"⚙️ Config: {user}",
        description=f"Status: {status_display}",
        color=get_status_color(status)
    )
    
    # Add admin indicator if user is admin
    if is_admin(interaction.user.id):
        embed.description += " (Admin View)"
    
    # Add owner info
    owner_id = config.get('ownerId')
    if owner_id:
        embed.add_field(
            name="👤 Owner",
            value=f"<@{owner_id}> (`{owner_id}`)",
            inline=True
        )
    else:
        embed.add_field(
            name="👤 Owner",
            value="⚠️ Not set",
            inline=True
        )
    
    # Add debug info
    embed.add_field(
        name="🔍 Debug Info",
        value=f"Process Running: {is_process_running(user)}\nDetected Status: {status}",
        inline=True
    )
    
    # Add port info to embed
    ports = config.get('ports', {})
    embed.add_field(
        name="🔌 Port Configuration",
        value=f"**Chrome**: `{ports.get('chrome', 'N/A')}`\n**WebSocket**: `{ports.get('ws', 'N/A')}`\n**TCP**: `{ports.get('tcp', 'N/A')}`",
        inline=False
    )
    
    if servers:
        for i, entry in enumerate(servers, 1):
            embed.add_field(
                name=f"#{i} {entry['serverId']}",
                value=(f"**Delay**: `{entry['delay']}ms`\n"
                       f"**Claim**: `{entry['claimMessage']}`\n"
                       f"**Keywords**: `{', '.join(entry['keywords'])}`"),
                inline=False
            )
    else:
        embed.add_field(name="No Servers", value="Click 'Add Server' to get started", inline=False)

    class ActionButton(Button):
        def __init__(self, label, style, custom_id, disabled=False):
            super().__init__(label=label, style=style, custom_id=custom_id, disabled=disabled)
        
        async def callback(self, button_interaction: Interaction):
            try:
                # Check if user can manage this config
                config = load_config(user)
                if not can_manage_config(button_interaction.user.id, config):
                    await button_interaction.response.send_message("❌ You don't have permission to manage this config!", ephemeral=True)
                    return
                
                if self.custom_id == 'start':
                    success, message = start_process(user)
                    emoji = "🟢" if success else "❌"
                    await button_interaction.response.send_message(f"{emoji} {message}", ephemeral=True)
                    # Refresh the panel
                    await open_user_panel(button_interaction, user)
                
                elif self.custom_id == 'stop':
                    success, message = stop_process(user)
                    emoji = "🟡" if success else "❌"
                    await button_interaction.response.send_message(f"{emoji} {message}", ephemeral=True)
                    # Refresh the panel
                    await open_user_panel(button_interaction, user)
                
                elif self.custom_id == 'kill':
                    success, message = kill_process(user)
                    emoji = "🔴" if success else "❌"
                    await button_interaction.response.send_message(f"{emoji} {message}", ephemeral=True)
                    # Refresh the panel
                    await open_user_panel(button_interaction, user)
                
                elif self.custom_id == 'add':
                    await button_interaction.response.send_modal(AddModal(user))
                
                elif self.custom_id == 'edit':
                    await show_edit_options(button_interaction, user)
                
                elif self.custom_id == 'delete':
                    await show_delete_options(button_interaction, user)
                
                elif self.custom_id == 'set_owner':
                    await button_interaction.response.send_modal(SetOwnerModal(user))
                
                elif self.custom_id == 'back_to_admin':
                    await show_admin_dashboard(button_interaction)
            
            except Exception as e:
                print(f"Error in button callback: {e}")
                if not button_interaction.response.is_done():
                    await button_interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

    view = View(timeout=300)
    
    # Improved button logic
    print(f"[DEBUG] {user} status for buttons: {status}")
    
    # Start/Resume button: enabled when stopped, paused, or unknown
    start_disabled = status == 'active' or not user_can_manage
    start_label = "▶️ Resume" if status == 'paused' else "▶️ Start"
    view.add_item(ActionButton(start_label, ButtonStyle.success, "start", start_disabled))
    
    # Pause button: enabled when active, paused, or unknown (if process exists)
    stop_disabled = status == 'stopped' or not user_can_manage
    stop_label = "⏸️ Pause" if status == 'active' else "⏹️ Stop"
    view.add_item(ActionButton(stop_label, ButtonStyle.secondary, "stop", stop_disabled))
    
    # Kill button: enabled when process exists
    kill_disabled = not is_process_running(user) or not user_can_manage
    view.add_item(ActionButton("🚫 Force Kill", ButtonStyle.danger, "kill", kill_disabled))
    
    view.add_item(ActionButton("➕ Add Server", ButtonStyle.primary, "add", not user_can_manage))
    view.add_item(ActionButton("✏️ Edit Server", ButtonStyle.secondary, "edit", not servers or not user_can_manage))
    view.add_item(ActionButton("🗑️ Delete Server", ButtonStyle.secondary, "delete", not servers or not user_can_manage))
    
    # Add set owner button for configs without owners (admin only)
    if not owner_id and is_admin(interaction.user.id):
        view.add_item(ActionButton("👤 Set Owner", ButtonStyle.primary, "set_owner"))
    
    # Add back button for admin
    if is_admin(interaction.user.id):
        view.add_item(ActionButton("🔙 Back to Dashboard", ButtonStyle.secondary, "back_to_admin"))
    
    if hasattr(interaction, 'response') and not interaction.response.is_done():
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

# Show edit options
async def show_edit_options(interaction: Interaction, user: str):
    config = load_config(user)
    servers = config.get('servers', [])
    
    embed = Embed(title="✏️ Edit Server", description="Select a server to edit:", color=0xFFA500)
    
    options = []
    for i, entry in enumerate(servers):
        options.append(SelectOption(
            label=entry['serverId'],
            description=f"Delay: {entry['delay']}ms | Keywords: {len(entry['keywords'])}",
            value=str(i)
        ))
    
    class ServerSelect(Select):
        def __init__(self):
            super().__init__(placeholder="Choose server to edit...", options=options)
        
        async def callback(self, select_inter: Interaction):
            server_index = int(self.values[0])
            await show_field_options(select_inter, user, server_index)
    
    view = View(timeout=300)
    view.add_item(ServerSelect())
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# Show field edit options
async def show_field_options(interaction: Interaction, user: str, server_index: int):
    config = load_config(user)
    servers = config.get('servers', [])
    server = servers[server_index]
    
    embed = Embed(
        title=f"✏️ Edit: {server['serverId']}",
        description="What would you like to edit?",
        color=0xFFA500
    )
    
    embed.add_field(name="Current Settings", value=(
        f"**Delay**: {server['delay']}ms\n"
        f"**Claim**: {server['claimMessage']}\n"
        f"**Keywords**: {', '.join(server['keywords'])}"
    ), inline=False)
    
    class FieldButton(Button):
        def __init__(self, label, field_name):
            super().__init__(label=label, style=ButtonStyle.secondary)
            self.field_name = field_name
        
        async def callback(self, ctx: Interaction):
            await ctx.response.send_modal(EditFieldModal(user, server_index, self.field_name))
    
    view = View(timeout=300)
    view.add_item(FieldButton("⏱️ Edit Delay", "delay"))
    view.add_item(FieldButton("💬 Edit Claim Message", "claim"))
    view.add_item(FieldButton("🔍 Edit Keywords", "keywords"))
    view.add_item(FieldButton("🆔 Edit Server ID", "serverId"))
    
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# Show delete options
async def show_delete_options(interaction: Interaction, user: str):
    config = load_config(user)
    servers = config.get('servers', [])
    
    embed = Embed(title="🗑️ Delete Server", description="Select a server to delete:", color=0xFF0000)
    
    options = []
    for i, entry in enumerate(servers):
        options.append(SelectOption(
            label=entry['serverId'],
            description=f"Delay: {entry['delay']}ms | Keywords: {len(entry['keywords'])}",
            value=str(i)
        ))
    
    class DeleteSelect(Select):
        def __init__(self):
            super().__init__(placeholder="Choose server to delete...", options=options)
        
        async def callback(self, select_inter: Interaction):
            server_index = int(self.values[0])
            await confirm_delete(select_inter, user, server_index)
    
    view = View(timeout=300)
    view.add_item(DeleteSelect())
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# Confirm delete
async def confirm_delete(interaction: Interaction, user: str, server_index: int):
    config = load_config(user)
    servers = config.get('servers', [])
    server = servers[server_index]
    
    embed = Embed(
        title="⚠️ Confirm Delete",
        description=f"Are you sure you want to delete **{server['serverId']}**?",
        color=0xFF0000
    )
    
    class ConfirmButton(Button):
        def __init__(self, confirm: bool):
            if confirm:
                super().__init__(label="✅ Yes, Delete", style=ButtonStyle.danger)
            else:
                super().__init__(label="❌ Cancel", style=ButtonStyle.secondary)
            self.confirm = confirm
        
        async def callback(self, ctx: Interaction):
            if self.confirm:
                config = load_config(user)
                servers = config.get('servers', [])
                deleted_server = servers.pop(server_index)
                config['servers'] = servers
                save_config(user, config)
                await ctx.response.send_message(f"🗑️ Deleted server: {deleted_server['serverId']}", ephemeral=True)
            else:
                await ctx.response.send_message("❌ Cancelled", ephemeral=True)
    
    view = View(timeout=300)
    view.add_item(ConfirmButton(True))
    view.add_item(ConfirmButton(False))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

# Modal to set owner
class SetOwnerModal(Modal):
    def __init__(self, user):
        super().__init__(title="👤 Set Configuration Owner")
        self.user = user
        self.add_item(TextInput(
            label="Discord User ID",
            placeholder="Enter the Discord user ID of the owner...",
            max_length=20
        ))

    async def on_submit(self, interaction: Interaction):
        try:
            owner_id = int(self.children[0].value.strip())
            
            config = load_config(self.user)
            config['ownerId'] = owner_id
            save_config(self.user, config)
            
            await interaction.response.send_message(f"✅ Set owner of **{self.user}** to <@{owner_id}>", ephemeral=True)
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid Discord ID! Must be a number.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# Modal to add server
class AddModal(Modal):
    def __init__(self, user):
        super().__init__(title="➕ Add New Server")
        self.user = user
        self.add_item(TextInput(label="Server ID", placeholder="Enter server ID...", max_length=100))
        self.add_item(TextInput(label="Delay (ms)", placeholder="1000", max_length=10))
        self.add_item(TextInput(label="Claim Message", placeholder="Enter claim command...", max_length=500))
        self.add_item(TextInput(label="Keywords", placeholder="keyword1, keyword2, keyword3...", style=discord.TextStyle.paragraph))

    async def on_submit(self, interaction: Interaction):
        try:
            entry = {
                'serverId': self.children[0].value.strip(),
                'delay': int(self.children[1].value.strip()),
                'claimMessage': self.children[2].value.strip(),
                'keywords': [k.strip() for k in self.children[3].value.split(',') if k.strip()]
            }
            
            config = load_config(self.user)
            servers = config.get('servers', [])
            
            # Check for duplicate server ID
            if any(e['serverId'] == entry['serverId'] for e in servers):
                await interaction.response.send_message("❌ Server ID already exists!", ephemeral=True)
                return
            
            servers.append(entry)
            config['servers'] = servers
            save_config(self.user, config)
            await interaction.response.send_message(f"✅ Server **{entry['serverId']}** added successfully!", ephemeral=True)
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid delay value! Must be a number.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# Modal to edit individual fields
class EditFieldModal(Modal):
    def __init__(self, user, server_index, field_name):
        super().__init__(title=f"✏️ Edit {field_name.title()}")
        self.user = user
        self.server_index = server_index
        self.field_name = field_name
        
        config = load_config(user)
        servers = config.get('servers', [])
        server = servers[server_index]
        
        if field_name == "delay":
            self.add_item(TextInput(label="Delay (ms)", default=str(server['delay']), max_length=10))
        elif field_name == "claim":
            self.add_item(TextInput(label="Claim Message", default=server['claimMessage'], max_length=500))
        elif field_name == "keywords":
            self.add_item(TextInput(
                label="Keywords (comma separated)",
                default=', '.join(server['keywords']),
                style=discord.TextStyle.paragraph
            ))
        elif field_name == "serverId":
            self.add_item(TextInput(label="Server ID", default=server['serverId'], max_length=100))

    async def on_submit(self, interaction: Interaction):
        try:
            config = load_config(self.user)
            servers = config.get('servers', [])
            server = servers[self.server_index]
            
            new_value = self.children[0].value.strip()
            
            if self.field_name == "delay":
                server['delay'] = int(new_value)
            elif self.field_name == "claim":
                server['claimMessage'] = new_value
            elif self.field_name == "keywords":
                server['keywords'] = [k.strip() for k in new_value.split(',') if k.strip()]
            elif self.field_name == "serverId":
                # Check for duplicate server ID
                if any(e['serverId'] == new_value for i, e in enumerate(servers) if i != self.server_index):
                    await interaction.response.send_message("❌ Server ID already exists!", ephemeral=True)
                    return
                server['serverId'] = new_value
            
            config['servers'] = servers
            save_config(self.user, config)
            await interaction.response.send_message(f"✅ {self.field_name.title()} updated successfully!", ephemeral=True)
            
        except ValueError:
            await interaction.response.send_message("❌ Invalid value! Please check your input.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# Error handler
@bot.event
async def on_error(event, *args, **kwargs):
    print(f"Error in {event}: {args}, {kwargs}")

# Ready event
@bot.event
async def on_ready():
    print(f"✅ {bot.user} is ready!")
    
    # Set bot status to online with "watching ." activity
    activity = Activity(type=ActivityType.watching, name=".")
    await bot.change_presence(status=discord.Status.online, activity=activity)
    
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} command(s)")
        print(f"✅ Bot status set to: Watching tyler")
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}")

# Run the bot
if __name__ == "__main__":
    bot.run(os.getenv('BOT_TOKEN'))
