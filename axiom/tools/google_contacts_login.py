"""Local Google consent, separate from Calendar. See docs/google-contacts.md."""
import argparse
import json
from pathlib import Path

SCOPE = 'https://www.googleapis.com/auth/contacts.readonly'


def main():
    parser = argparse.ArgumentParser(description='Подключить чтение Google Контактов')
    parser.add_argument('--credentials', type=Path, required=True,
                        help='OAuth client JSON (Desktop или Web с localhost redirect)')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    data = json.loads(args.credentials.read_text(encoding='utf-8-sig'))
    if 'web' in data and f'http://localhost:{args.port}/' not in data['web'].get('redirect_uris', []):
        parser.error(f'Добавьте http://localhost:{args.port}/ в Authorized redirect URIs OAuth-клиента '
                     'и скачайте JSON заново, либо используйте OAuth-клиент типа Desktop.')
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(args.credentials), [SCOPE])
    creds = flow.run_local_server(port=args.port, access_type='offline', prompt='consent',
                                 timeout_seconds=180,
                                 authorization_prompt_message='Открываю Google для разрешения чтения контактов.',
                                 success_message='Доступ получен. Вкладку можно закрыть.')
    out = Path(__file__).resolve().parents[1] / 'google_contacts_token.json'
    out.write_text(creds.to_json(), encoding='utf-8')
    print(f'Готово: {out}\nВ AXIOM: Контакты → Google Контакты → Подключение → Загрузить файл доступа.')


if __name__ == '__main__':
    main()
