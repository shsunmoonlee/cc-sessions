-- cc-sessions menu bar item: one click restores the tabs a dead iTerm2 instance had open.
--   "cc"     nothing to restore
--   "cc ⟲N"  N sessions of the last iTerm2 instance are not open
-- `make install` copies this file to ~/.hammerspoon/. Required from init.lua inside a pcall,
-- so an error here never breaks the rest of the config:
--   local ok, menu = pcall(require, "cc_sessions_menu"); if ok then menu.start() end

local M = {}

local HOME = os.getenv("HOME")
local CLI = HOME .. "/.claude/bin/cc-sessions"
local LOG = HOME .. "/Library/Logs/cc-sessions.log"
local PREVIEW = HOME .. "/Library/Logs/cc-sessions-restore-preview.log"
local REFRESH_EVERY = 60
local OLDER_SHOWN = 5

local st = { known = false, pending = 0, time = "", path = "", restoring = false, snapshots = {} }
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

local function refresh()
  run({ "tabs", "--json" }, function(code, out)
    if code ~= 0 then return end
    local ok, d = pcall(hs.json.decode, out)
    if not ok or type(d) ~= "table" then return end
    st.known = d.pending_known == true
    st.pending = tonumber(d.pending) or 0
    st.time = d.pending_time or ""
    st.path = d.pending_path or ""
    st.restoring = d.restoring == true
    st.snapshots = d.snapshots or {}
    render()
  end)
end

local function notify(text)
  hs.notify.new({ title = "cc-sessions", informativeText = text }):send()
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
    if s.path ~= st.path and (s.before_shrink or not s.is_current) then
      table.insert(sub, {
        title = (s.time or "?") .. ": " .. (s.tabs or 0) .. " tabs" .. (s.before_shrink and " (before a shrink)" or ""),
        fn = function() restore({ "restore", "--snapshot", s.path }) end,
      })
    end
  end
  return sub
end

local function buildMenu()
  local items = {}
  local working = busy or st.restoring
  if not st.known then
    table.insert(items, { title = "iTerm2 could not be asked: restore unavailable", disabled = true })
  elseif st.path == "" then
    table.insert(items, { title = "No earlier snapshot to restore", disabled = true })
  else
    table.insert(items, {
      title = "Restore " .. st.pending .. " sessions from " .. st.time,
      disabled = working or st.pending == 0,
      fn = function() restore({ "restore" }) end,
    })
  end
  table.insert(items, { title = "Preview restore", disabled = working, fn = preview })
  local older = olderItems()
  if #older > 0 then
    table.insert(items, { title = "Older snapshots", disabled = working, menu = older })
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
  render()
  refresh()
  M.timer = hs.timer.doEvery(REFRESH_EVERY, refresh)
end

return M
