"""Render sourced media references only; never fetch or download media."""
import html


def _url(value):
    from .single_session_document import _safe_url
    try:
        return isinstance(value, str) and bool(_safe_url(value))
    except ValueError:
        return False


def media_text(value, inline):
    text = str(value)
    # Media metadata is not a financial content channel. In particular, new
    # ledger-only markers must not escape canonical document price accounting.
    if '{{price:' in text:
        raise ValueError('media captions cannot introduce price markers')
    return inline(text)


def cover_html(data, inline):
    from .single_session_detail import valid_images
    for day in data.get('days', []):
        for spot in day.get('attractions', []):
            images = valid_images(spot)
            if not images:
                continue
            item = images[0]
            title = media_text(spot.get('name') or '目的地', inline)
            caption = media_text(item.get('caption') or '目的地风光；拍摄时节见出处', inline)
            return ('<figure class="handbook-cover"><img fetchpriority="high" '
                    'referrerpolicy="no-referrer" src="' + html.escape(item['url'], quote=True)
                    + '" alt="目的地封面" onerror="this.hidden=true">'
                    '<figcaption>' + title + ' · ' + caption + ' · <a href="'
                    + html.escape(item['source_url'], quote=True)
                    + '" rel="noreferrer noopener">图片出处</a></figcaption></figure>')
    return ''


def video_html(spot, inline):
    """Writer's source assessment is required; no autoplay, iframe or download."""
    for item in spot.get('videos', []) if isinstance(spot.get('videos'), list) else []:
        if not isinstance(item, dict):
            continue
        if (item.get('source_type') not in ('official', 'scenic_official')
                or not item.get('authority_basis') or not item.get('publisher')
                or not all(_url(item.get(key)) for key in ('url', 'source_url', 'poster_url'))):
            continue
        esc = lambda value: html.escape(str(value), quote=True)
        return ('<figure class="destination-video"><a target="_blank" rel="noreferrer noopener" '
                'href="' + esc(item['url']) + '"><img loading="lazy" referrerpolicy="no-referrer" '
                'src="' + esc(item['poster_url']) + '" alt="景区视频封面"'
                + ' onerror="this.hidden=true"><span>在来源网站播放</span></a><figcaption>'
                + media_text(item.get('title') or spot.get('name') or '景区视频', inline) + ' · <a href="'
                + esc(item['source_url']) + '" rel="noreferrer noopener">'
                + media_text(item['publisher'], inline) + '</a></figcaption></figure>')
    return ''
