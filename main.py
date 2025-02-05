#!/bin/python3
import logging
import argparse
import hashlib
import time
import sys
import urllib.parse
import re
import eyed3
from eyed3.id3.frames import ImageFrame
from typing import Optional
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from requests import Session
from pathlib import Path
import datetime as dt

MD5_SALT = 'XGRlBW9FXlekgbPrRHuSiA'
ENCODED_BY = 'https://github.com/llistochek/yandex-music-downloader'
DEFAULT_USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/106.0.0.0 Safari/537.36'
DEFAULT_DELAY = 3
DEFAULT_PATH_PATTERN = Path('#album-artist', '#album', '#number - #title')
DEFAULT_COVER_RESOLUTION = 400
DEFAULT_LOG_LEVEL = 'INFO'

TRACK_RE = re.compile(r'track/(\d+)$')
ALBUM_RE = re.compile(r'album/(\d+)$')
ARTIST_RE = re.compile(r'artist/(\d+)$')
PLAYLIST_RE = re.compile(r'([\w\-]+)/playlists/(\d+)$')

FILENAME_CLEAR_RE = re.compile(r'[^\w\-\'() ]+')

TITLE_FMT = '%(title)s (%(version)s)'


@dataclass
class PlaylistId:
    owner: str
    kind: int


def parse_artists(artists: list) -> list[str]:
    artists_names = []
    for artist in artists:
        artists_names.append(artist['name'])
        if decomposed := artist.get('decomposed'):
            for d_artist in decomposed:
                if isinstance(d_artist, dict):
                    artists_names.append(d_artist['name'])
    return artists_names


def parse_title(json: dict) -> str:
    title = json['title']
    if version := json.get('version'):
        title = TITLE_FMT % {'title': title, 'version': version}
    return title


@dataclass
class BasicAlbumInfo:
    id: str
    title: str
    release_date: Optional[dt.datetime]
    year: int
    artists: list[str]

    @staticmethod
    def from_json(json: dict):
        if json['metaType'] != 'music':
            logging.info('"%s" пропущен т.к. не является музыкальным альбомом', json['title'])
            return None
        artists = parse_artists(json['artists'])
        title = parse_title(json)
        release_date = json.get('releaseDate')
        if release_date is not None:
            release_date = dt.datetime.fromisoformat(release_date)
        return BasicAlbumInfo(id=json['id'], title=title, year=json['year'],
                              artists=artists, release_date=release_date)


@dataclass
class BasicTrackInfo:
    title: str
    id: str
    real_id: str
    album: BasicAlbumInfo
    number: int
    disc_number: int
    artists: list[str]
    url_template: str
    has_lyrics: bool

    @staticmethod
    def from_json(json: dict):
        if not json['available']:
            return None
        album_json = json['albums'][0]
        artists = parse_artists(json['artists'])
        track_position = album_json['trackPosition']
        album = BasicAlbumInfo.from_json(album_json)
        if album is None:
            raise ValueError
        url_template = 'https://' + json['ogImage']
        has_lyrics = json['lyricsInfo']['hasAvailableTextLyrics']
        title = parse_title(json)
        return BasicTrackInfo(title=title, id=str(json['id']), real_id=json['realId'],
                              number=track_position['index'], disc_number=track_position['volume'],
                              artists=artists, album=album, url_template=url_template,
                              has_lyrics=has_lyrics)

    def pic_url(self, resolution: int) -> str:
        return self.url_template.replace('%%', f'{resolution}x{resolution}')


@dataclass
class FullTrackInfo(BasicTrackInfo):
    lyrics: str

    @staticmethod
    def from_json(json: dict):
        base = BasicTrackInfo.from_json(json['track'])
        lyrics = json['lyric'][0]['fullLyrics']
        return FullTrackInfo(**base.__dict__, lyrics=lyrics)


@dataclass
class FullAlbumInfo(BasicAlbumInfo):
    tracks: list[BasicTrackInfo]

    @staticmethod
    def from_json(json: dict):
        base = BasicAlbumInfo.from_json(json)
        tracks = json.get('volumes', [])
        tracks = [t for v in tracks for t in v]
        tracks = map(BasicTrackInfo.from_json, tracks)
        tracks = [t for t in tracks if t is not None]
        return FullAlbumInfo(**base.__dict__, tracks=tracks)


@dataclass
class ArtistInfo:
    id: str
    name: str
    albums: list[BasicAlbumInfo]
    url_template: str

    @staticmethod
    def from_json(json: dict):
        artist = json['artist']
        albums = map(BasicAlbumInfo.from_json, json.get('albums', []))
        albums = [a for a in albums if a is not None]
        url_template = 'https://' + artist['ogImage']
        return ArtistInfo(id=str(artist['id']), name=artist['name'],
                          albums=albums, url_template=url_template)

    def pic_url(self, resolution: int) -> str:
        return self.url_template.replace('%%', f'{resolution}x{resolution}')


def get_track_download_url(session: Session, track: BasicTrackInfo, hq: bool) -> str:
    resp = session.get('https://music.yandex.ru/api/v2.1/handlers/track'
                       f'/{track.id}:{track.album.id}'
                       '/web-album_track-track-track-main/download/m'
                       f'?hq={int(hq)}')
    url_info_src = resp.json()['src']

    resp = session.get('https:' + url_info_src)
    url_info = ET.fromstring(resp.text)
    path = url_info.find('path').text[1:]
    s = url_info.find('s').text
    ts = url_info.find('ts').text
    host = url_info.find('host').text
    path_hash = hashlib.md5((MD5_SALT + path + s).encode()).hexdigest()
    return 'https://%s/get-mp3/%s/%s/%s?track-id=%s' \
           % (host, path_hash, ts, path, track.id)


def download_file(session: Session, url: str, path: Path) -> None:
    resp = session.get(url)
    with open(path, 'wb') as f:
        for chunk in resp.iter_content(chunk_size=1024):
            f.write(chunk)


def download_bytes(session: Session, url: str) -> bytes:
    resp = session.get(url)
    return resp.content


def get_full_track_info(session: Session, track_id: str) -> FullTrackInfo:
    params = {
        'track': track_id,
        'lang': 'ru'
    }
    resp = session.get(f'https://music.yandex.ru/handlers/track.jsx', params=params)
    return FullTrackInfo.from_json(resp.json())


def get_full_album_info(session: Session, album_id: str) -> FullAlbumInfo:
    params = {
        'album': album_id,
        'lang': 'ru'
    }
    resp = session.get(f'https://music.yandex.ru/handlers/album.jsx', params=params)
    return FullAlbumInfo.from_json(resp.json())


def get_artist_info(session: Session, artist_id: str) -> ArtistInfo:
    params = {
        'artist': artist_id,
        'what': 'albums',
        'lang': 'ru'
    }
    resp = session.get('https://music.yandex.ru/handlers/artist.jsx', params=params)
    return ArtistInfo.from_json(resp.json())


def get_playlist(session: Session, playlist: PlaylistId) -> list[BasicTrackInfo]:
    params = {
        'owner': playlist.owner,
        'kinds': playlist.kind,
        'lang': 'ru'
    }
    resp = session.get('https://music.yandex.ru/handlers/playlist.jsx', params=params)
    raw_tracks = resp.json()['playlist'].get('tracks')
    if raw_tracks is None:
        logging.error('Нет треков в плейлисте')
        return  None
    tracks = map(BasicTrackInfo.from_json, raw_tracks)
    tracks = [t for t in tracks if t is not None]
    return tracks


def prepare_track_path(path: Path, prepare_path: bool, track: BasicTrackInfo) -> Path:
    path_str = str(path)
    repl_dict = {
        '#number': track.number,
        '#artist': track.artists[0],
        '#album-artist': track.album.artists[0],
        '#title': track.title,
        '#album': track.album.title,
        '#year': track.album.year
    }
    for placeholder, replacement in repl_dict.items():
        replacement = str(replacement)
        if prepare_path:
            replacement = FILENAME_CLEAR_RE.sub('_', replacement)
        path_str = path_str.replace(placeholder, replacement)
    path_str += '.mp3'
    return Path(path_str)


def set_id3_tags(path: Path, track: BasicTrackInfo, lyrics: Optional[str],
                 album_cover: Optional[bytes]) -> None:
    if track.album.release_date is not None:
        release_date = eyed3.core.Date(*track.album.release_date.timetuple()[:6])
    else:
        release_date = track.album.year
    audiofile = eyed3.load(path)
    audiofile.tag = tag = eyed3.id3.tag.Tag()
    tag.artist = chr(0).join(track.artists)
    tag.album_artist = track.album.artists[0]
    tag.album = track.album.title
    tag.title = track.title
    tag.track_num = track.number
    tag.disc_num = track.disc_number
    tag.release_date = tag.original_release_date = release_date
    tag.encoded_by = ENCODED_BY

    if lyrics is not None:
        tag.lyrics.set(lyrics)
    if album_cover is not None:
        tag.images.set(ImageFrame.FRONT_COVER, album_cover, 'image/jpeg')

    tag.save()


if __name__ == '__main__':
    eyed3.log.setLevel("ERROR")
    parser = argparse.ArgumentParser(description='Загрузчик музыки с сервиса Яндекс.Музыка')

    def help_str(text: Optional[str] = None) -> str:
        default = 'по умолчанию: %(default)s'
        if text is None:
            return default
        return f'{text} ({default})'

    common_group = parser.add_argument_group('Общие параметры')
    common_group.add_argument('--hq', action='store_true',
                              help='Загружать треки в высоком качестве')
    common_group.add_argument('--skip-existing', action='store_true',
                              help='Пропускать уже загруженные треки')
    common_group.add_argument('--add-lyrics', action='store_true',
                              help='Загружать тексты песен')
    common_group.add_argument('--embed-cover', action='store_true',
                              help='Встраивать обложку в .mp3 файл')
    common_group.add_argument('--stick-to-artist', action='store_true',
                              help='Загружать только альбомы созданные данным исполнителем')
    common_group.add_argument('--cover-resolution', default=DEFAULT_COVER_RESOLUTION,
                              metavar='<Разрешение обложки>', type=int, help=help_str(None))
    common_group.add_argument('--delay', default=DEFAULT_DELAY, metavar='<Задержка>',
                              type=int, help=help_str('Задержка между запросами, в секундах'))
    common_group.add_argument('--log-level', default=DEFAULT_LOG_LEVEL,
                              choices=logging._nameToLevel.keys())

    def args_playlist_id(arg: str) -> PlaylistId:
        arr = arg.split('/')
        return PlaylistId(owner=arr[0], kind=int(arr[1]))
    id_group_meta = parser.add_argument_group('ID')
    id_group = id_group_meta.add_mutually_exclusive_group(required=True)
    id_group.add_argument('--artist-id', metavar='<ID исполнителя>')
    id_group.add_argument('--album-id', metavar='<ID альбома>')
    id_group.add_argument('--track-id', metavar='<ID трека>')
    id_group.add_argument('--playlist-id', type=args_playlist_id,
                          metavar='<владелец плейлиста>/<тип плейлиста>')
    id_group.add_argument('-u', '--url', help='URL исполнителя/альбома/трека/плейлиста')

    path_group = parser.add_argument_group('Указание пути')
    path_group.add_argument('--strict-path', action='store_true',
                            help='Очищать путь от недопустимых символов')
    path_group.add_argument('--dir', default='.', metavar='<Папка>',
                            help=help_str('Папка для загрузки музыки'), type=Path)
    path_group.add_argument('--path-pattern', default=DEFAULT_PATH_PATTERN,
                            metavar='<Паттерн>', type=Path,
                            help=help_str('Поддерживает следующие заполнители:'
                                          ' #number, #artist, #album-artist, #title,'
                                          ' #album, #year'))

    auth_group = parser.add_argument_group('Авторизация')
    auth_group.add_argument('--session-id', required=True, metavar='<ID сессии>')
    auth_group.add_argument('--user-agent', default=DEFAULT_USER_AGENT, metavar='<User-Agent>',
                            help=help_str())

    args = parser.parse_args()
    logging.basicConfig(format='%(asctime)s |%(levelname)s| %(message)s',
                        datefmt='%H:%M:%S', level=args.log_level.upper())

    def response_hook(resp, *_args, **_kwargs):
        if logging.root.isEnabledFor(logging.DEBUG):
            if 'application/json' in resp.headers['Content-Type']:
                logging.debug(resp.text)
        time.sleep(args.delay)

    session = Session()
    session.hooks = {'response': response_hook}
    session.cookies.set('Session_id', args.session_id, domain='yandex.ru')
    session.headers['User-Agent'] = args.user_agent
    session.headers['X-Retpath-Y'] = urllib.parse.quote_plus('https://music.yandex.ru')

    result_tracks: list[BasicTrackInfo] = []

    if args.url is not None:
        if match := ARTIST_RE.search(args.url):
            args.artist_id = match.group(1)
        elif match := ALBUM_RE.search(args.url):
            args.album_id = match.group(1)
        elif match := TRACK_RE.search(args.url):
            args.track_id = match.group(1)
        elif match := PLAYLIST_RE.search(args.url):
            args.playlist_id = PlaylistId(owner=match.group(1), kind=int(match.group(2)))
        else:
            print('Параметер url указан в неверном формате')
            sys.exit(1)

    if args.artist_id is None and args.stick_to_artist:
        logging.warning('Флаг --stick-to-artist имеет смысл только при'
                        ' загрузке всех треков исполнителя')

    if args.artist_id is not None:
        artist_info = get_artist_info(session, args.artist_id)
        albums_count = 0
        for album in artist_info.albums:
            if args.stick_to_artist and album.artists[0] != artist_info.name:
                logging.info('Альбом "%s" пропущен из-за флага --stick-to-artist', album.title)
                continue
            full_album = get_full_album_info(session, album.id)
            result_tracks.extend(full_album.tracks)
            albums_count += 1
        logging.info(artist_info.name)
        logging.info('Альбомов: %d', albums_count)
    elif args.album_id is not None:
        album = get_full_album_info(session, args.album_id)
        logging.info(album.title)
        result_tracks = album.tracks
    elif args.track_id is not None:
        result_tracks = [get_full_track_info(session, args.track_id)]
    elif args.playlist_id is not None:
        result_tracks = get_playlist(session, args.playlist_id)

    logging.info('Треков: %d', len(result_tracks))
    covers: dict[str, bytes] = {}

    for track in result_tracks:
        album = track.album
        save_path = args.dir / prepare_track_path(args.path_pattern, args.strict_path, track)
        if args.skip_existing and save_path.is_file():
            continue

        save_dir = save_path.parent
        if not save_dir.is_dir():
            save_dir.mkdir(parents=True)

        url = get_track_download_url(session, track, args.hq)
        logging.info('Загружается %s', save_path)
        download_file(session, url, save_path)

        lyrics = None
        if args.add_lyrics and track.has_lyrics:
            if isinstance(track, FullTrackInfo):
                lyrics = track.lyrics
            else:
                full_info = get_full_track_info(session, track.id)
                lyrics = full_info.lyrics

        cover = None
        if args.embed_cover:
            if cached_cover := covers.get(album.id):
                cover = cached_cover
            else:
                cover = covers[album.id] = download_bytes(session,
                                                          track.pic_url(args.cover_resolution))
        else:
            cover_path = save_dir / 'cover.jpg'
            if not cover_path.is_file():
                download_file(session, track.pic_url(args.cover_resolution), cover_path)

        set_id3_tags(save_path, track, lyrics, cover)
