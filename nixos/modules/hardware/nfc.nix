{ ... }:
{
  services.neard = {
    enable = true;
    settings.General = {
      # Keep the adapter unpowered and passive polling disabled at idle to avoid
      # holding PN547 VEN / RPMH_LN_BB_CLK2 active; neard powers it on demand.
      ConstantPoll = false;
      DefaultPowered = false;
      ResetOnError = true;
    };
  };

  systemd.services.neard.wantedBy = [ "multi-user.target" ];
}
