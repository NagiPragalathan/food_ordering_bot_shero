// The dashboard's stylesheet: app/static/css/admin.css (every admin page and
// the sign-in page). Built by `npm run build` here, and again in the Docker
// image, from the classes the admin templates use.
const brand = require("./brand");

module.exports = {
  content: {
    relative: true,
    files: ["../app/templates/admin/**/*.html", "../app/templates/_brand.html",
            "../app/static/admin/**/*.js"],
  },
  theme: {
    extend: {
      colors: {
        ...brand,
        // Every admin page is built on Tailwind's `stone` greys. Here they are
        // a teal-tinted grey from the logo, so all pages pick up the brand
        // from this one place instead of each template changing.
        stone: { 50: "#F6FAFA", 100: "#EDF4F3", 200: "#DCE8E7", 300: "#C2D4D3", 400: "#8BA3A1",
                 500: "#627B79", 600: "#475F5D", 700: "#324846", 800: "#203533", 900: "#122523" },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
      },
      boxShadow: {
        card: "0 1px 2px rgba(13,31,30,.04), 0 6px 20px -8px rgba(13,31,30,.10)",
      },
    },
  },
};
