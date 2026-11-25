-- SPDX-License-Identifier: MIT

local log = Log.open_topic ("s-alphaplus-bluetooth-sco")
local lutils = require ("linking-utils")
local cutils = require ("common-utils")
local hardware = {}
local watched = setmetatable ({}, { __mode = "k" })
local rates = { cvsd = 8000, msbc = 16000 }

local function updateOffload (device_id, acquire)
  local active = false
  for _, entry in pairs (hardware) do
    if entry.device == device_id and entry.active then
      active = true
      break
    end
  end
  if active and not acquire then return end
  local device = cutils.get_object_manager ("device"):lookup {
    type = "device", Constraint { "object.id", "=", device_id },
  }
  if device then
    device:set_params ("Props", Pod.Object {
      "Spa:Pod:Object:Param:Props", "Props", bluetoothOffloadActive = active,
    })
  end
end

local function createPCM (id, entry)
  local pcm = Node ("adapter", {
    ["factory.name"] = entry.playback and "api.alsa.pcm.sink" or "api.alsa.pcm.source",
    ["api.alsa.path"] = "alphaplus_bluetooth",
    ["api.alsa.disable-mmap"] = true,
    ["api.alsa.soft-mixer"] = true,
    ["media.class"] = entry.playback and "Audio/Sink/Internal" or "Audio/Source/Internal",
    ["node.name"] = "alsa_" .. entry.name,
    ["node.description"] = "WCN3998 SCO PCM",
    ["priority.session"] = 0,
    ["audio.format"] = "S16LE",
    ["audio.rate"] = entry.rate,
    ["audio.channels"] = 1,
    ["audio.position"] = "[MONO]",
  })
  if not pcm then
    hardware [id] = nil
    updateOffload (entry.device)
    log:error ("Failed to create SCO PCM for " .. entry.name)
    return
  end
  entry.pcm = pcm
  pcm:activate (Features.ALL, function (_, err)
    if hardware [id] ~= entry or entry.pcm ~= pcm then return end
    if err then
      pcm:request_destroy ()
      hardware [id] = nil
      updateOffload (entry.device)
      log:error ("Failed to activate SCO PCM: " .. tostring (err))
      return
    end
    log:info ("Created " .. entry.name .. " PCM at " .. tostring (entry.rate) .. " Hz")
  end)
end

local function updateReadyPCMs (device)
  local ready = false
  for p in device:iterate_params ("Props") do
    local params = cutils.parseParam (p, "Props").params
    for i = 1, #params, 2 do
      if params [i] == "bluez5.hw-offload-ready" and params [i + 1] then
        ready = true
      end
    end
  end
  for id, entry in pairs (hardware) do
    if entry.device == device ["bound-id"] then
      if ready and entry.active and not entry.pcm then
        createPCM (id, entry)
      elseif not ready and entry.pcm then
        entry.pcm:request_destroy ()
        entry.pcm = nil
      end
    end
  end
end

SimpleEventHook {
  name = "monitor/alphaplus-create-sco-pcm",
  before = "node/create-item",
  interests = { EventInterest {
    Constraint { "event.type", "=", "node-added" },
    Constraint { "node.name", "#", "bluez_*", type = "pw-global" },
    Constraint { "api.bluez5.profile", "=", "headset-head-unit", type = "pw-global" },
  } },
  execute = function (event)
    local node = event:get_subject ()
    local properties = node.properties
    local rate = rates [properties ["api.bluez5.codec"]]
    if not rate then
      log:error ("Unsupported WCN3998 SCO codec: " .. tostring (properties ["api.bluez5.codec"]))
      return
    end
    local entry = {
      name = properties ["node.name"], rate = rate,
      playback = properties ["media.class"] == "Audio/Sink",
      device = properties:get_int ("device.id"),
      active = false,
    }
    hardware [node.id] = entry
    local device = cutils.get_object_manager ("device"):lookup {
      type = "device", Constraint { "object.id", "=", entry.device },
    }
    if not watched [device] then
      watched [device] = true
      device:connect ("params-changed", function (device, param)
        if param == "Props" then updateReadyPCMs (device) end
      end)
    end
    device:activate (Features.ALL, function (_, err)
      if hardware [node.id] ~= entry then return end
      if err then
        hardware [node.id] = nil
        updateOffload (entry.device)
        log:error ("Failed to monitor SCO transport: " .. tostring (err))
        return
      end
      updateReadyPCMs (device)
    end)
  end,
}:register ()

SimpleEventHook {
  name = "monitor/alphaplus-sco-active",
  interests = { EventInterest {
    Constraint { "event.type", "=", "node-state-changed" },
    Constraint { "node.name", "#", "bluez_*", type = "pw-global" },
    Constraint { "api.bluez5.profile", "=", "headset-head-unit", type = "pw-global" },
  } },
  execute = function (event)
    local entry = hardware [event:get_subject ().id]
    if entry then
      entry.active = event:get_properties () ["event.subject.new-state"] == "running"
      updateOffload (entry.device, true)
      local device = cutils.get_object_manager ("device"):lookup {
        type = "device", Constraint { "object.id", "=", entry.device },
      }
      if device then updateReadyPCMs (device) end
    end
  end,
}:register ()

SimpleEventHook {
  name = "monitor/alphaplus-remove-sco-pcm",
  interests = { EventInterest {
    Constraint { "event.type", "=", "node-removed" },
    Constraint { "node.name", "#", "bluez_*", type = "pw-global" },
  } },
  execute = function (event)
    local id = event:get_subject ().id
    local entry = hardware [id]
    if entry then
      hardware [id] = nil
      if entry.pcm then entry.pcm:request_destroy () end
      updateOffload (entry.device)
    end
  end,
}:register ()

SimpleEventHook {
  name = "linking/alphaplus-sco-target",
  before = "linking/find-defined-target",
  interests = { EventInterest {
    Constraint { "event.type", "=", "select-target" },
    Constraint { "node.name", "#", "*.bluez_*put*" },
    Constraint { "media.class", "#", "Stream/*" },
  } },
  execute = function (event)
    local _, om, _, properties, flags = lutils:unwrap_select_target_event (event)
    local name = properties ["node.name"]:match ("^output%.(bluez_output%..+)$") or
        properties ["node.name"]:match ("^input%.(bluez_input%..+)$")
    if not name then return end
    local target = om:lookup {
      type = "SiLinkable", Constraint { "node.name", "=", "alsa_" .. name },
    }
    if not target then
      -- Registering the PCM session item triggers the standard linking rescan.
      event:stop_processing ()
      return
    end
    flags.has_defined_target = true
    flags.has_node_defined_target = true
    flags.can_passthrough = false
    event:set_data ("target", target)
  end,
}:register ()
