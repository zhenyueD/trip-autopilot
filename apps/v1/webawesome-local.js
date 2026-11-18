// 只注册用到的组件（五件套 + dialog + progress-bar + carousel 两件套），其余组件不进页面。
// token 映射在 studio-wa-tokens.css，组件不许原样上脸。
import './vendor/webawesome/components/icon/icon.js';
import './vendor/webawesome/components/badge/badge.js';
import './vendor/webawesome/components/details/details.js';
import './vendor/webawesome/components/card/card.js';
import './vendor/webawesome/components/skeleton/skeleton.js';
import './vendor/webawesome/components/callout/callout.js';
import './vendor/webawesome/components/dialog/dialog.js';
import './vendor/webawesome/components/progress-bar/progress-bar.js';
import './vendor/webawesome/components/carousel/carousel.js';
import './vendor/webawesome/components/carousel-item/carousel-item.js';

// 桥接：JS 动态创建的 <summary> 默认会落进 wa-details 的内容区，
// 统一在这里补 slot="summary"（静态 HTML 已直接写好 slot，幂等）。
function slotSummaries(root) {
  const targets = root?.querySelectorAll?.('wa-details > summary:not([slot])') || [];
  targets.forEach(summary => summary.setAttribute('slot', 'summary'));
}
if (typeof document !== 'undefined') {
  slotSummaries(document);
  new MutationObserver(records => {
    records.forEach(record => record.addedNodes.forEach(node => {
      if (node.nodeType !== 1) return;
      if (node.tagName === 'SUMMARY' && node.parentElement?.tagName === 'WA-DETAILS' && !node.slot) {
        node.setAttribute('slot', 'summary');
      }
      slotSummaries(node);
    }));
  }).observe(document.documentElement, { childList: true, subtree: true });
}

