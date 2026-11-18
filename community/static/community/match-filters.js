const filters = document.querySelector(".match-filters");

if (filters && typeof filters.requestSubmit === "function") {
    filters.querySelector('button[type="submit"]').hidden = true;
    filters.addEventListener("change", (event) => {
        if (event.target.matches("select")) {
            filters.requestSubmit();
        }
    });
}
