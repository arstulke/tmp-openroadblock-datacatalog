// Progressive enhancement for the feed list: search/region filter and
// "Copy URL" buttons. Without JS the page still lists every feed.
(() => {
  const form = document.querySelector(".filters");
  const q = document.getElementById("q");
  const region = document.getElementById("region");
  const shown = document.getElementById("shown");
  const empty = document.getElementById("empty");
  const groups = [...document.querySelectorAll(".group")];
  const total = document.querySelectorAll(".feed").length;
  form.hidden = false;

  function apply() {
    const terms = q.value.toLowerCase().split(/\s+/).filter(Boolean);
    let count = 0;
    for (const group of groups) {
      const inRegion = !region.value || group.dataset.state === region.value;
      let visible = 0;
      for (const li of group.querySelectorAll(".feed")) {
        const match = inRegion && terms.every(t => li.dataset.search.includes(t));
        li.hidden = !match;
        visible += match;
      }
      group.hidden = visible === 0;
      count += visible;
    }
    shown.textContent = count === total ? `${total} feeds` : `${count} of ${total} feeds`;
    empty.hidden = count > 0;
  }
  q.addEventListener("input", apply);
  region.addEventListener("change", apply);
  apply();

  for (const btn of document.querySelectorAll("button.copy")) {
    if (!navigator.clipboard) continue;
    const use = btn.querySelector("use");
    const label = btn.querySelector("span");
    btn.hidden = false;
    btn.addEventListener("click", async () => {
      await navigator.clipboard.writeText(btn.dataset.url);
      use.setAttribute("href", "#i-check");
      label.textContent = "Copied";
      setTimeout(() => {
        use.setAttribute("href", "#i-copy");
        label.textContent = "Copy URL";
      }, 1500);
    });
  }
})();
