// metacomputer: Firefox's own cookie banner handling answers the banners it knows with
// "reject all" (1: reject only; it never accepts on the user's behalf). A default, not locked:
// a profile can change it in about:config.
pref("cookiebanners.service.mode", 1);
pref("cookiebanners.service.mode.privateBrowsing", 1);
