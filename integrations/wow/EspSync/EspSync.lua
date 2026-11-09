-- EspSync: shows the player's own health and level-ups as a thin strip of
-- solid-colour pixels in the top-left corner of the screen.  EspSync (the
-- room-lighting app) reads the strip from its screen capture and pulses the
-- lights red at low health and sweeps them gold on a level-up.
--
-- Output only: the addon draws pixels and nothing else.  It reads only the
-- player's own unit, never acts on the game and has no network or file access
-- (WoW addons can't have either).
--
-- Strip layout, left to right, in physical screen pixels (CELL = 4):
--   cell 0      magenta            marker
--   cell 1      green              marker
--   cell 2, 3   data, 3 bits each  red = 1, green = 2, blue = 4
--                                  bits 0-3 level-up counter (mod 16)
--                                  bit 4 dead or ghost, bit 5 test health active
--   cell 4      check              cell2 XOR cell3 XOR 5
--   cells 5-20  health bar         yellow fill on blue, filled from the left
--   cell 21     cyan               end marker
-- Health is a status bar rather than a number because in Midnight (12.x) and
-- Forever UnitHealth("player") is a secret value: addons can't do maths on it,
-- but status bars can still display it.  Keep this layout in sync with
-- espsync/wow.py.

local CELL = 4
local HEADER_CELLS = 5
local BAR_CELLS = 16
local TOTAL_CELLS = HEADER_CELLS + BAR_CELLS + 1
local TEST_SECONDS = 15

local floor = math.floor

local levelups = 0
local testHealth = nil -- 0..1 while "/espsync test lowhp" is running
local testToken = 0

local function isSecret(value)
  return issecretvalue ~= nil and issecretvalue(value)
end

-- ------------------------------------------------------------------ frames
local strip = CreateFrame("Frame", "EspSyncStrip", UIParent)
strip:SetFrameStrata("TOOLTIP")
strip:SetFrameLevel(9000)
strip:SetPoint("TOPLEFT", UIParent, "TOPLEFT", 0, 0)
strip:SetSize(TOTAL_CELLS * CELL, CELL)

local function block(firstCell, cells, r, g, b)
  local tex = strip:CreateTexture(nil, "ARTWORK")
  tex:SetPoint("TOPLEFT", strip, "TOPLEFT", firstCell * CELL, 0)
  tex:SetSize(cells * CELL, CELL)
  tex:SetColorTexture(r, g, b, 1)
  return tex
end

block(0, 1, 1, 0, 1) -- magenta
block(1, 1, 0, 1, 0) -- green
local dataLo = block(2, 1, 0, 0, 0)
local dataHi = block(3, 1, 0, 0, 0)
local check = block(4, 1, 0, 0, 0)
block(HEADER_CELLS + BAR_CELLS, 1, 0, 1, 1) -- cyan

-- The bar is anchored to the strip, never the other way round: a secret
-- value on the bar must not spread to the other textures.
local bar = CreateFrame("StatusBar", nil, strip)
bar:SetPoint("TOPLEFT", strip, "TOPLEFT", HEADER_CELLS * CELL, 0)
bar:SetSize(BAR_CELLS * CELL, CELL)
bar:SetStatusBarTexture("Interface\\Buttons\\WHITE8X8")
bar:SetStatusBarColor(1, 1, 0, 1)
local barBg = bar:CreateTexture(nil, "BACKGROUND")
barBg:SetAllPoints(bar)
barBg:SetColorTexture(0, 0, 1, 1)
bar:SetMinMaxValues(0, 1)
bar:SetValue(1)

-- One strip unit = one physical pixel, whatever the UI scale.
local function updateScale()
  local _, height = GetPhysicalScreenSize()
  local parentScale = UIParent:GetEffectiveScale()
  if height and height > 0 and parentScale and parentScale > 0 then
    strip:SetScale(768 / height / parentScale)
  end
end

-- ------------------------------------------------------------------ state
local function setBits(tex, value)
  tex:SetColorTexture(value % 2, floor(value / 2) % 2, floor(value / 4) % 2, 1)
end

local function updateData()
  local dead = UnitIsDeadOrGhost("player")
  if isSecret(dead) then
    dead = false
  end
  local value = (levelups % 16) + (dead and 16 or 0) + (testHealth and 32 or 0)
  local lo, hi = value % 8, floor(value / 8)
  setBits(dataLo, lo)
  setBits(dataHi, hi)
  setBits(check, bit.bxor(bit.bxor(lo, hi), 5))
end

local function updateHealth()
  if testHealth then
    bar:SetMinMaxValues(0, 1)
    bar:SetValue(testHealth)
    return
  end
  -- Secret or not, both values go straight to the status bar; no maths here.
  local maxHealth = UnitHealthMax("player")
  if not isSecret(maxHealth) and (maxHealth == nil or maxHealth <= 0) then
    maxHealth = 1
  end
  bar:SetMinMaxValues(0, maxHealth)
  bar:SetValue(UnitHealth("player"))
end

local function updateAll()
  updateScale()
  updateHealth()
  updateData()
end

-- ------------------------------------------------------------------ events
local events = CreateFrame("Frame")
events:RegisterEvent("PLAYER_ENTERING_WORLD")
events:RegisterEvent("PLAYER_LEVEL_UP")
events:RegisterEvent("PLAYER_DEAD")
events:RegisterEvent("PLAYER_ALIVE")
events:RegisterEvent("PLAYER_UNGHOST")
events:RegisterEvent("UI_SCALE_CHANGED")
events:RegisterEvent("DISPLAY_SIZE_CHANGED")
events:RegisterUnitEvent("UNIT_HEALTH", "player")
events:RegisterUnitEvent("UNIT_MAXHEALTH", "player")
pcall(events.RegisterUnitEvent, events, "UNIT_HEALTH_FREQUENT", "player") -- older clients only

events:SetScript("OnEvent", function(_, event)
  if event == "UNIT_HEALTH" or event == "UNIT_MAXHEALTH" or event == "UNIT_HEALTH_FREQUENT" then
    updateHealth()
  elseif event == "PLAYER_LEVEL_UP" then
    levelups = levelups + 1
    updateData()
  elseif event == "UI_SCALE_CHANGED" or event == "DISPLAY_SIZE_CHANGED" then
    updateScale()
  else
    updateAll()
  end
end)

-- Catch anything the events missed (dead/ghost has no single event).
C_Timer.NewTicker(0.25, function()
  updateHealth()
  updateData()
end)

-- ------------------------------------------------------------------ slash command
local function say(text)
  print("|cff33ccffEspSync|r: " .. text)
end

local function startTestHealth(fraction)
  testHealth = fraction
  testToken = testToken + 1
  local token = testToken
  updateHealth()
  updateData()
  C_Timer.After(TEST_SECONDS, function()
    if token == testToken and testHealth then
      testHealth = nil
      updateAll()
      say("test health ended")
    end
  end)
end

SLASH_ESPSYNC1 = "/espsync"
SlashCmdList.ESPSYNC = function(msg)
  local cmd, arg, value = strsplit(" ", strlower(strtrim(msg or "")))
  if cmd == "test" and arg == "levelup" then
    levelups = levelups + 1
    updateData()
    say("test level-up sent")
  elseif cmd == "test" and arg == "lowhp" then
    local percent = tonumber(value) or 20
    percent = math.max(0, math.min(100, percent))
    startTestHealth(percent / 100)
    say(("showing %d%% health for %d seconds"):format(percent, TEST_SECONDS))
  elseif cmd == "test" and arg == "off" then
    testToken = testToken + 1
    testHealth = nil
    updateAll()
    say("test health off")
  elseif cmd == "hide" then
    strip:Hide()
    say("strip hidden (EspSync game effects pause)")
  elseif cmd == "show" then
    strip:Show()
    updateAll()
    say("strip shown")
  else
    say(("strip %s, %dx%d px in the top-left corner"):format(
      strip:IsShown() and "shown" or "hidden", TOTAL_CELLS * CELL, CELL))
    say("/espsync test levelup  - play the level-up effect")
    say("/espsync test lowhp [percent]  - pretend health is low for " .. TEST_SECONDS .. " s (default 20)")
    say("/espsync test off  - stop the health test")
    say("/espsync hide | show  - hide or show the strip")
  end
end

updateAll()
