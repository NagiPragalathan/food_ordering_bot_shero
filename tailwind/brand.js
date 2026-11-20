// Shero brand colours, shared by the ordering pages and the dashboard.
//
// They come from the logo (docs/brand/shero-logo-original.png): teal #029B99
// and the red dot #C7010A.
//
//   brand   the logo teal - headers, buttons, links, selected states
//   accent  the logo red - small highlights only, as in the logo
//   ink     dark text, teal-tinted so it sits with the brand
//   mist    page backgrounds and borders
module.exports = {
  brand:  { 50: "#E6F5F5", 100: "#C4E9E8", 200: "#93D6D4", 300: "#5CC0BD", 400: "#2DAEAB",
            500: "#029B99", 600: "#028582", 700: "#026B69", 800: "#03524F", 900: "#043A38" },
  accent: { 50: "#FDECEC", 100: "#F9CDCE", 500: "#C7010A", 600: "#A80109", 700: "#870108" },
  ink:    { 700: "#25403F", 800: "#172E2D", 900: "#0D1F1E" },
  mist:   { 50: "#F8FBFB", 100: "#EFF6F5", 200: "#DCE9E8" },
};
