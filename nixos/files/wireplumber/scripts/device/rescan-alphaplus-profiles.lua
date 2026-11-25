SimpleEventHook {
  name = "device/rescan-alphaplus-profiles",
  before = { "device/select-route", "linking/rescan-trigger" },
  interests = {
    EventInterest {
      Constraint { "event.type", "=", "device-params-changed" },
      Constraint { "event.subject.param-id", "=", "EnumRoute" },
      Constraint { "device.name", "=", "alsa_card.platform-sound" },
    },
  },
  execute = function (event)
    -- Route availability can arrive after the profile availability notification.
    event:get_source ():call ("push-event", "select-profile", event:get_subject (), nil)
  end
}:register ()
