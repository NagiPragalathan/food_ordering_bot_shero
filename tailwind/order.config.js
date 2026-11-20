// The customer pages' stylesheet: app/static/css/order.css (the menu,
// checkout, address and expired-link pages). Built by `npm run build` here,
// and again in the Docker image, from the classes the order templates use.
const brand = require("./brand");

module.exports = {
  content: {
    relative: true,
    files: ["../app/templates/order/**/*.html", "../app/templates/_brand.html"],
  },
  theme: {
    extend: {
      colors: {
        // Logo teal on near-white, the logo red for small highlights.
        ...brand,
        // Herb green stays for "veg" and success.
        herb: { 500: "#3E8E5A", 600: "#2F7549", 700: "#245C39" },
      },
      boxShadow: {
        card: "0 1px 2px rgba(13,31,30,.06), 0 4px 16px rgba(13,31,30,.05)",
      },
    },
  },
};
