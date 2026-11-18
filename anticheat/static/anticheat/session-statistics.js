(() => {
    let pending;

    document.addEventListener("click", async (event) => {
        const link = event.target.closest(".stats-switcher a");
        if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey ||
            event.ctrlKey || event.shiftKey || event.altKey) {
            return;
        }
        event.preventDefault();

        const panel = link.closest(".session-statistics");
        const error = panel.querySelector(".stats-error");
        error.hidden = true;
        pending?.abort();
        if (link.getAttribute("aria-current") === "page") {
            return;
        }

        const controller = new AbortController();
        pending = controller;
        panel.setAttribute("aria-busy", "true");
        const url = new URL(link.href);

        try {
            const response = await fetch(url, {
                headers: {"X-Session-Statistics": "fragment"},
                signal: controller.signal,
            });
            if (!response.ok || response.redirected) {
                throw new Error("Statistics request failed");
            }
            const documentPart = new DOMParser().parseFromString(await response.text(), "text/html");
            const replacement = documentPart.querySelector(".session-statistics");
            if (!replacement) {
                throw new Error("Statistics fragment missing");
            }
            if (controller.signal.aborted) {
                return;
            }

            const moveFocus = panel.contains(document.activeElement);
            history.replaceState(history.state, "", url);
            panel.replaceWith(replacement);
            document.querySelectorAll("a[data-stats-context]").forEach((contextLink) => {
                const contextUrl = new URL(contextLink.href);
                contextUrl.searchParams.set("stats", url.searchParams.get("stats"));
                contextLink.href = contextUrl.href;
            });
            if (moveFocus) {
                replacement.querySelector('[aria-current="page"]').focus({preventScroll: true});
            }
        } catch {
            if (!controller.signal.aborted) {
                error.hidden = false;
            }
        } finally {
            if (pending === controller) {
                pending = null;
                panel.removeAttribute("aria-busy");
            }
        }
    });
})();
