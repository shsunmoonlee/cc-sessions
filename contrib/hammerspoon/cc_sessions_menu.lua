-- cc-sessions menu bar item: one click restores the tabs a dead iTerm2 instance had open, and the
-- tab tools (find, sort, close, pin, group) run from here. Nothing moves or closes a tab unless
-- one of these items or hotkeys is used.
--   "cc"     nothing to restore
--   "cc ⟲N"  N sessions of the last iTerm2 instance are not open
-- `make install` copies this file to ~/.hammerspoon/. Required from init.lua inside a pcall,
-- so an error here never breaks the rest of the config:
--   local ok, menu = pcall(require, "cc_sessions_menu"); if ok then menu.start() end

local M = {}

local HOME = os.getenv("HOME")
local CLI = HOME .. "/.claude/bin/cc-sessions"
local TABS = HOME .. "/.claude/bin/cc-tabs"
local LOG = HOME .. "/Library/Logs/cc-sessions.log"
local PREVIEW = HOME .. "/Library/Logs/cc-sessions-restore-preview.log"
local REFRESH_EVERY = 60
local OLDER_SHOWN = 5
local CLOSE_DAYS = 7
local HYPER = { "ctrl", "alt", "cmd" }
local FIND_KEY, SORT_KEY = "F", "S"
local HYPER_TEXT = "⌃⌥⌘"
local ASK_KEY = "cc_sessions.ask_iterm"
local COLORS = {
  { "Red", "#d0453c" }, { "Orange", "#e0822d" }, { "Yellow", "#d9b62c" }, { "Green", "#4ca14f" },
  { "Blue", "#3f7fd6" }, { "Purple", "#8a56c9" }, { "Pink", "#d6609f" }, { "Gray", "#808890" },
}

local st = {
  known = false, pending = 0, time = "", path = "", dismissed = false, restoring = false, snapshots = {},
  idleTabs = 0, idleKnown = false, groups = {},
}
local busy = false

-- Running tasks are held here until they exit: an unreferenced hs.task can be collected mid-run,
-- and its callback (which clears `busy`) would never fire.
M.tasks = {}

local function runExe(exe, args, cb)
  local t
  t = hs.task.new(exe, function(code, out, err)
    M.tasks[t] = nil
    if cb then cb(code, out or "", err or "") end
  end, args)
  if t and t:start() then
    M.tasks[t] = true
  elseif cb then
    cb(-1, "", "could not start " .. exe)
  end
end

local function run(args, cb)
  runExe(CLI, args, cb)
end

local function runTabs(args, cb)
  runExe(TABS, args, cb)
end

local function lastLine(text)
  local line = ""
  for l in text:gmatch("[^\r\n]+") do line = l end
  return line
end

local function render()
  if not M.menu then return end
  local text = "cc"
  if busy or st.restoring then
    text = "cc ..."
  elseif st.pending > 0 then
    text = "cc ⟲" .. st.pending
  end
  M.menu:setTitle(hs.styledtext.new(text, { font = hs.styledtext.defaultFonts.menuBar }))
end

-- The minute refresh never connects to iTerm2's API: no cookie, no websocket, no iterm2 import.
-- Its one AppleScript call (the live tab list) waits for the first time the owner uses this menu,
-- so the Automation prompt answers a click instead of appearing when Hammerspoon loads.
local function refresh()
  local args = { "tabs", "--json", "--idle-days", tostring(CLOSE_DAYS) }
  if not hs.settings.get(ASK_KEY) then table.insert(args, "--no-ask") end
  run(args, function(code, out)
    if code ~= 0 then return end
    local ok, d = pcall(hs.json.decode, out)
    if not ok or type(d) ~= "table" then return end
    st.known = d.pending_known == true
    st.pending = tonumber(d.pending) or 0
    st.time = d.pending_time or ""
    st.path = d.pending_path or ""
    st.dismissed = d.pending_dismissed == true
    st.restoring = d.restoring == true
    st.snapshots = d.snapshots or {}
    st.idleTabs = tonumber(d.idle_tabs) or 0
    st.idleKnown = d.idle_tabs_known == true
    render()
  end)
  runTabs({ "group", "list", "--json" }, function(code, out)
    if code ~= 0 then return end
    local ok, d = pcall(hs.json.decode, out)
    if ok and type(d) == "table" then st.groups = d end
  end)
end

-- -> true the first time ever.
local function allowAsking()
  if hs.settings.get(ASK_KEY) then return false end
  hs.settings.set(ASK_KEY, true)
  refresh()
  return true
end

local function notify(text)
  hs.notify.new({ title = "cc-sessions", informativeText = text }):send()
end

-- Tell what a command did, and show the menu's cached facts the result.
local function report(code, out, err)
  local line = lastLine(out)
  if code ~= 0 or line == "" then line = lastLine(err) end
  if line == "" then line = "exited " .. code end
  notify(line)
  refresh()
end

local function tabsCommand(args)
  return function() runTabs(args, report) end
end

local function restore(args)
  if busy or st.restoring then hs.alert.show("A restore is already running", 2) return end
  busy = true
  render()
  run(args, function(code, out, err)
    busy = false
    local line = lastLine(out)
    if line == "" then line = lastLine(err) end
    if line == "" then line = "restore exited " .. code end
    notify(line)
    refresh()
    render()
  end)
end

local function preview()
  run({ "restore", "--dry-run" }, function(code, out, err)
    local text = out .. err
    hs.alert.show(lastLine(text), 4)
    local fh = io.open(PREVIEW, "w")
    if not fh then return end
    fh:write(text)
    fh:close()
    runExe("/usr/bin/open", { "-a", "Console", PREVIEW })
  end)
end

local function olderItems()
  local sub = {}
  for _, s in ipairs(st.snapshots) do
    if #sub >= OLDER_SHOWN then break end
    if (s.path ~= st.path or st.dismissed) and (s.before_shrink or not s.is_current) then
      table.insert(sub, {
        title = (s.time or "?") .. ": " .. (s.tabs or 0) .. " tabs" .. (s.before_shrink and " (before a shrink)" or ""),
        fn = function() restore({ "restore", "--snapshot", s.path }) end,
      })
    end
  end
  return sub
end

local function age(t)
  local s = math.max(0, os.time() - (tonumber(t) or 0))
  if s < 3600 then return math.floor(s / 60) .. "m" end
  if s < 172800 then return math.floor(s / 3600) .. "h" end
  return math.floor(s / 86400) .. "d"
end

-- Title goes in `text` and group and folder in `subText`, both searched (searchSubText).
local function pickerChoices(rows)
  local out = {}
  for _, d in ipairs(rows) do
    local parts = { age(d.activity), d.state or "?" }
    if d.group and d.group ~= "" then table.insert(parts, d.group) end
    if d.folder and d.folder ~= "" then table.insert(parts, d.folder) end
    table.insert(out, { text = d.title or d.session_id, subText = table.concat(parts, " · "), sessionId = d.session_id })
  end
  return out
end

local function openPicker()
  allowAsking()
  runTabs({ "find", "--json" }, function(code, out, err)
    local ok, d = pcall(hs.json.decode, out)
    if code ~= 0 or not ok or type(d) ~= "table" then
      notify("Find session failed: " .. lastLine(err))
      return
    end
    M.chooser:choices(pickerChoices(d))
    M.chooser:show()
  end)
end

local function sortTabs()
  allowAsking()
  runTabs({ "sort" }, report)
end

-- The title's number is an estimate. The confirmation names the exact dry run, and the close is
-- limited to that dry run's sessions, so it cannot close more than it said. Cancel is the default.
local function closeIdle()
  runTabs({ "close-idle", "--dry-run", "--json", "--days", tostring(CLOSE_DAYS) }, function(code, out, err)
    local ok, d = pcall(hs.json.decode, out)
    if code ~= 0 or not ok or type(d) ~= "table" or type(d.session_ids) ~= "table" then
      notify("Close idle tabs failed: " .. lastLine(err))
      return
    end
    local n = #d.session_ids
    st.idleTabs, st.idleKnown = n, true
    if n == 0 then
      notify("No tabs idle over " .. CLOSE_DAYS .. " days")
      return
    end
    local closeLabel = "Close " .. n .. " tabs"
    hs.focus()
    local choice = hs.dialog.blockAlert(closeLabel .. " idle over " .. CLOSE_DAYS .. " days?",
      "Their sessions stay hibernated: Find session and wake still reach them.", "Cancel", closeLabel)
    if choice == closeLabel then
      runTabs({ "close-idle", "--days", tostring(CLOSE_DAYS), "--only", table.concat(d.session_ids, ",") }, report)
    end
  end)
end

local function promptName(message, default)
  hs.focus()
  local button, text = hs.dialog.textPrompt(message, "", default or "", "OK", "Cancel")
  if button ~= "OK" then return nil end
  text = (text or ""):gsub("^%s+", ""):gsub("%s+$", "")
  if text == "" then return nil end
  return text
end

local function groupThisTabItems()
  local sub = {}
  for _, g in ipairs(st.groups) do
    table.insert(sub, { title = g.name, fn = tabsCommand({ "group", "add", g.name }) })
  end
  if #sub > 0 then table.insert(sub, { title = "-" }) end
  table.insert(sub, {
    title = "New group...",
    fn = function()
      local name = promptName("New group for this tab")
      if name then runTabs({ "group", "add", name }, report) end
    end,
  })
  table.insert(sub, { title = "Remove from group", fn = tabsCommand({ "group", "remove" }) })
  return sub
end

local function colorItems(name)
  local sub = {}
  for _, c in ipairs(COLORS) do
    table.insert(sub, { title = c[1], fn = tabsCommand({ "group", "color", name, c[2] }) })
  end
  table.insert(sub, { title = "None", fn = tabsCommand({ "group", "color", name, "none" }) })
  return sub
end

local function groupsItems()
  local sub = {}
  for _, g in ipairs(st.groups) do
    table.insert(sub, {
      title = g.name .. " (" .. (g.members or 0) .. ")",
      menu = {
        {
          title = "Rename...",
          fn = function()
            local name = promptName("Rename group " .. g.name, g.name)
            if name then runTabs({ "group", "rename", g.name, name }, report) end
          end,
        },
        { title = "Color", menu = colorItems(g.name) },
        {
          title = "Delete...",
          fn = function()
            hs.focus()
            local choice = hs.dialog.blockAlert("Delete group " .. g.name .. "?",
              "Its tabs stay open and become ungrouped.", "Cancel", "Delete")
            if choice == "Delete" then runTabs({ "group", "delete", g.name }, report) end
          end,
        },
      },
    })
  end
  return sub
end

local function buildMenu()
  local items = {}
  local working = busy or st.restoring
  local firstOpen = allowAsking()
  if not st.known then
    table.insert(items, {
      title = firstOpen and "Asking iTerm2 for its tabs..." or "iTerm2 could not be asked: restore unavailable",
      disabled = true,
    })
  elseif st.path == "" then
    table.insert(items, { title = "No earlier snapshot to restore", disabled = true })
  else
    table.insert(items, {
      title = "Restore " .. st.pending .. " sessions from " .. st.time,
      disabled = working or st.pending == 0,
      fn = function() restore({ "restore" }) end,
    })
    table.insert(items, {
      title = "Dismiss",
      disabled = working or st.pending == 0,
      fn = function() run({ "restore", "--dismiss" }, report) end,
    })
  end
  table.insert(items, { title = "Preview restore", disabled = working, fn = preview })
  local older = olderItems()
  if #older > 0 then
    table.insert(items, { title = "Older snapshots", disabled = working, menu = older })
  end
  table.insert(items, { title = "-" })
  table.insert(items, { title = "Find session (" .. HYPER_TEXT .. FIND_KEY .. ")", fn = openPicker })
  table.insert(items, { title = "Sort tabs (" .. HYPER_TEXT .. SORT_KEY .. ")", disabled = working, fn = sortTabs })
  table.insert(items, {
    title = st.idleKnown and ("Close about " .. st.idleTabs .. " tabs idle over " .. CLOSE_DAYS .. " days")
      or ("Close tabs idle over " .. CLOSE_DAYS .. " days"),
    disabled = working or (st.idleKnown and st.idleTabs == 0),
    fn = closeIdle,
  })
  table.insert(items, { title = "-" })
  table.insert(items, { title = "Pin or unpin this tab", fn = tabsCommand({ "pin", "--toggle" }) })
  table.insert(items, { title = "Group this tab", menu = groupThisTabItems() })
  local groups = groupsItems()
  if #groups > 0 then
    table.insert(items, { title = "Groups", menu = groups })
  end
  table.insert(items, { title = "-" })
  table.insert(items, {
    title = "Open log",
    fn = function() runExe("/usr/bin/open", { "-a", "Console", LOG }) end,
  })
  return items
end

function M.start()
  M.menu = hs.menubar.new(true, "cc-sessions")
  if not M.menu then return end
  M.menu:setTooltip("Claude Code sessions: restore the tabs a dead iTerm2 instance had open")
  M.menu:setMenu(buildMenu)
  M.chooser = hs.chooser.new(function(choice)
    if choice then runTabs({ "open", choice.sessionId }, function(code, out, err)
      if code ~= 0 then notify(lastLine(err)) end
    end) end
  end)
  M.chooser:searchSubText(true)
  M.hotkeys = { hs.hotkey.bind(HYPER, FIND_KEY, openPicker), hs.hotkey.bind(HYPER, SORT_KEY, sortTabs) }
  render()
  refresh()
  M.timer = hs.timer.doEvery(REFRESH_EVERY, refresh)
end

return M
