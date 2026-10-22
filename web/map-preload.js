/* Background preparation has no progress UI; only completed maps are adopted. */
(() => {
  let checking = false;
  async function adoptPreparedMap() {
    if (checking || document.hidden || state.page !== 'work' || state.view !== 'map' || state.job) return;
    const id = state.article?.id;
    if (!id) return;
    checking = true;
    try {
      const latest = await api('article/' + id);
      if (state.article?.id !== id || state.view !== 'map' || state.job) return;
      if (latest.map && latest.map_input !== state.article.map_input) {
        state.article.map = latest.map;
        state.article.quality = latest.quality;
        state.article.map_input = latest.map_input;
        renderWork();
      }
    } catch { /* A background miss must not interrupt reading. */ }
    finally { checking = false; }
  }
  const previousDispatch = dispatch;
  dispatch = async function(action) {
    if (action === 'generate' && state.article?.map) {
      const id = state.article.id;
      return runJob('map', {article_id: id, force: true}, async () => {
        if (state.article?.id === id) {
          state.article = await api('article/' + id);
          if (state.view === 'map') renderWork();
        }
        await refresh();
        toast('导图已生成');
      });
    }
    const result = await previousDispatch(action);
    if (action === 'mapview') void adoptPreparedMap();
    return result;
  };
  setInterval(adoptPreparedMap, 2500);
})();
