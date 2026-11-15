#!/usr/bin/env python3
"""Tronqueira Publicador: script completo de envio de um episódio ao canal Histórias da Tronqueira.

Uso interno (um usuário: o dono do canal). Fluxo:
  1. troca o refresh token OAuth 2.0 do dono do canal por um access token;
  2. videos.insert (envio retomável) com title, description, tags, categoryId, idioma,
     status.privacyStatus=private + publishAt (agendamento), selfDeclaredMadeForKids=false e
     containsSyntheticMedia=true (narração e imagens geradas por IA);
  3. thumbnails.set com a miniatura do episódio;
  4. captions.insert com a legenda .srt em pt-BR.

Credenciais em variáveis de ambiente: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN.
Uso: python3 tronqueira_publicador.py PASTA_DO_EPISODIO [2026-12-04T22:00:00Z]
A pasta tem metadados.json (titulo, descricao, tags, thumbnails, legenda), video_1080p.mp4,
thumbnail_1.jpg e legendas.srt.
"""
import json
import os
import urllib.parse
import urllib.request

TOKEN_URL = 'https://oauth2.googleapis.com/token'
API = 'https://www.googleapis.com/youtube/v3'
ANALYTICS = 'https://youtubeanalytics.googleapis.com/v2/reports'
ENVIO_LIBERADO = True


def token() -> str:
    corpo = urllib.parse.urlencode({'client_id': os.environ['YT_CLIENT_ID'],
                                    'client_secret': os.environ['YT_CLIENT_SECRET'],
                                    'refresh_token': os.environ['YT_REFRESH_TOKEN'],
                                    'grant_type': 'refresh_token'}).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=corpo), timeout=30) as r:
        return json.load(r)['access_token']


def get(url: str, params: dict, tk: str) -> dict:
    req = urllib.request.Request(url + '?' + urllib.parse.urlencode(params), headers={'Authorization': f'Bearer {tk}'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def meu_canal(tk: str) -> dict:
    r = get(f'{API}/channels', {'part': 'snippet,statistics,status', 'mine': 'true'}, tk)
    return (r.get('items') or [{}])[0]


# ---------------- envio (videos.insert, thumbnails.set, captions.insert) ----------------
UPLOAD = 'https://www.googleapis.com/upload/youtube/v3'
PEDACO = 8 * 1024 * 1024


def corpo_do_video(meta: dict, publicar_em: str | None) -> dict:
    """snippet + status de um episódio. publicar_em: ISO 8601 (UTC) para agendar; o vídeo sobe privado e o
    YouTube publica sozinho na hora. Conteúdo sintético declarado e não destinado a crianças, sempre."""
    status = {'privacyStatus': 'private', 'selfDeclaredMadeForKids': False, 'containsSyntheticMedia': True,
              'embeddable': True, 'license': 'youtube'}
    if publicar_em:
        status['publishAt'] = publicar_em
    return {'snippet': {'title': meta['titulo'][:100], 'description': meta['descricao'][:5000],
                        'tags': meta.get('tags') or [], 'categoryId': '22',
                        'defaultLanguage': 'pt-BR', 'defaultAudioLanguage': 'pt-BR'},
            'status': status}


def enviar_video(tk: str, arquivo, corpo: dict, http=None) -> str:
    """Envio retomável em pedaços de 8 MB. Devolve o id do vídeo."""
    import requests
    http = http or requests
    from pathlib import Path
    arquivo = Path(arquivo)
    total = arquivo.stat().st_size
    r = http.post(f'{UPLOAD}/videos', params={'uploadType': 'resumable', 'part': 'snippet,status'},
                  headers={'Authorization': f'Bearer {tk}', 'Content-Type': 'application/json; charset=UTF-8',
                           'X-Upload-Content-Type': 'video/mp4', 'X-Upload-Content-Length': str(total)},
                  data=json.dumps(corpo), timeout=60)
    r.raise_for_status()
    sessao = r.headers['Location']
    inicio = 0
    with arquivo.open('rb') as f:
        while True:
            f.seek(inicio)
            dados = f.read(PEDACO)
            fim = inicio + len(dados) - 1
            r = http.put(sessao, data=dados, timeout=300,
                         headers={'Authorization': f'Bearer {tk}', 'Content-Range': f'bytes {inicio}-{fim}/{total}'})
            if r.status_code in (200, 201):
                return r.json()['id']
            if r.status_code != 308:
                r.raise_for_status()
            faixa = r.headers.get('Range')          # 'bytes=0-8388607': o que o YouTube já recebeu
            inicio = int(faixa.split('-')[1]) + 1 if faixa else 0


def enviar_miniatura(tk: str, video_id: str, imagem, http=None) -> None:
    import requests
    http = http or requests
    from pathlib import Path
    r = http.post(f'{UPLOAD}/thumbnails/set', params={'videoId': video_id, 'uploadType': 'media'},
                  headers={'Authorization': f'Bearer {tk}', 'Content-Type': 'image/jpeg'},
                  data=Path(imagem).read_bytes(), timeout=120)
    r.raise_for_status()


def enviar_legenda(tk: str, video_id: str, srt, http=None) -> None:
    import requests
    http = http or requests
    from pathlib import Path
    fronteira = 'tronqueira-legenda'
    meta = json.dumps({'snippet': {'videoId': video_id, 'language': 'pt-BR', 'name': 'Português', 'isDraft': False}})
    corpo = (f'--{fronteira}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n'
             f'--{fronteira}\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + \
        Path(srt).read_bytes() + f'\r\n--{fronteira}--\r\n'.encode()
    r = http.post(f'{UPLOAD}/captions', params={'uploadType': 'multipart', 'part': 'snippet'},
                  headers={'Authorization': f'Bearer {tk}', 'Content-Type': f'multipart/related; boundary={fronteira}'},
                  data=corpo, timeout=120)
    r.raise_for_status()


def publicar_episodio(pasta, publicar_em: str | None = None, tk: str | None = None, http=None) -> dict:
    """Sobe o vídeo longo de uma pasta de episódio (metadados.json, video_1080p.mp4, thumbnail_1.jpg,
    legendas.srt). Recusa enquanto o projeto não passou na auditoria: antes dela o YouTube trava o vídeo
    como privado para sempre."""
    if not ENVIO_LIBERADO:
        raise RuntimeError('envio pela API desligado até a auditoria do projeto (ver docs/P8_PASSO_A_PASSO.md)')
    from pathlib import Path
    pasta = Path(pasta)
    meta = json.loads((pasta / 'metadados.json').read_text(encoding='utf-8'))
    tk = tk or token()
    vid = enviar_video(tk, pasta / 'video_1080p.mp4', corpo_do_video(meta, publicar_em), http)
    enviar_miniatura(tk, vid, pasta / meta['thumbnails'][0], http)
    if (pasta / meta.get('legenda', 'legendas.srt')).exists():
        enviar_legenda(tk, vid, pasta / meta.get('legenda', 'legendas.srt'), http)
    return {'video_id': vid, 'url': f'https://youtu.be/{vid}', 'publicar_em': publicar_em}


if __name__ == '__main__':
    import sys
    print(json.dumps(publicar_episodio(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None), ensure_ascii=False))
