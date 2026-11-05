"use strict";
// Progressive enhancement only: both explicit download choices always remain available.
const platform = navigator.userAgentData?.platform || navigator.platform || "";
const mac = document.querySelector(".download-mac");
const windows = document.querySelector(".download-windows");
if (/win/i.test(platform) && mac && windows) {
  windows.classList.add("primary");
  mac.classList.remove("primary");
}
