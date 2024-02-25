from datetime import timedelta, date
import os.path
import re
import pandas
from collections import OrderedDict
from operator import itemgetter
import uuid
import requests
import json

hosts = {"STAND1": "http://suggestions.dadata.ru/suggestions/api",
         "STAND2": "http://stand2.ru",
         "STAND3": "http://stand3.ru",
         }
stand = input('Укажите тестовый стенд: STAND1 / STAND2 / STAND3\n>>>').strip()
try:
    url = hosts[stand]
except KeyError:
    print(f'Стенд {stand} отсутствует в списке стендов.')
    exit()

xls_file = input('Укажите абсолютный путь к файлу с тест-кейсами. '
                 'Файл с запросами и ответами для ПСИ появится в этой же папке.\n >>>')

# это путь к создаваемому файлу с запросами и ответами
rest_file = '\\'.join(xls_file.split('\\')[:-1]) + '\\rests-for-uat.json'

# это флаг, позволяющий установить специальный эндпоинт
special_endpoint_flag = True


# метод, который ищет и парсит тела запросов из кейсов, если в TestData кейса несколько
# json структур с ключом request_id, то выбирается та, что идет после ключевых слов "Запрос" или "РЕСТ".
def search_json(content):
    pattern_key_words = re.compile(r'Запрос|РЕСТ', re.IGNORECASE)
    key_words_match = re.search(pattern_key_words, content)
    if key_words_match is not None:
        key_words_start_index = key_words_match.start()
        content = content[key_words_start_index:]
    pattern_request_id = re.compile(r'.*("request_id":).*')
    i = 0
    json_part = ''
    for x in content:
        if x == '{':
            i += 1
        elif x == '}':
            i -= 1
        if i > 0:
            json_part += x
        elif i == 0 and re.search(pattern_request_id, json_part):
            json_part += '}'
            break
        else:
            json_part = ''
    return json_part.replace('\n', '')


# метод, который парсит xlsx файл с кейсами из Jira. Если у кейса несколько шагов, берется первый.
# На вход получает следующие столбцы:
# Columns: [Key, Name, Status, Precondition, Objective, Folder, Priority, Component, Labels, Owner, Estimated Time,
#           Coverage(Issues), Coverage(Pages), Test Script(Step - by - Step) - Step, Test
#           Script(Step - by - Step) - Test Data, Test Script(Step - by - Step) - Expected Result, Test Script(Plain
#           Text), Test Script(BDD)]
# проверяет, нет ли в тест-кейсе вариабельности значений (у нас для них используется ключевое слово TestData)
# отдает кейсы, отсортированные по названию кейса в алфавитном порядке.
def parse_xlsx():
    wb = pandas.read_excel(xls_file, 'Sheet0')
    wb = wb.dropna(subset=['Key'])
    data_from_xls = []
    for i in range(0, len(wb.index)):
        case_key = wb.iloc[i]['Key']
        case_name = wb.iloc[i]['Name']
        row_rb = wb.iloc[i]['Test Script (Step-by-Step) - Test Data'].replace('\xa0', ' ')
        case_request_body = search_json(row_rb)
        test_data = re.search(r'TestData', case_request_body, re.IGNORECASE)
        if test_data is not None:
            print(f'ВНИМАНИЕ: В теле запроса присутствует вариабельность данных - есть TestData. '
                  f'файл для ПСИ не может быть создан без конкретных значений полей.\n'
                  f'Проверьте тело запроса в кейсе {case_key} {case_name}')
            exit()
        case_folder = wb.iloc[i]['Folder']
        case_coverage = wb.iloc[i]['Coverage (Issues)']
        data_from_xls.append([case_name, case_request_body, case_folder, case_coverage, case_key])
    data_from_xls = sorted(data_from_xls, key=itemgetter(2))
    return data_from_xls


# превращает переменные Postman (в формате {{key}}), если использовался копипаст его запросов,
# в конкретные значения (например, для даты, uuid, статуса),добавляет нужные и удаляет ненужные поля.
# для ПСИ файла сортирует ключи запроса, как это привычно человеческому восприятию (отдает сортированный словарь).
def beautify_request_body(request_body, case_key):
    request_id = f'{uuid.uuid4()}'
    doc_guid = f'{uuid.uuid4()}'
    doc_date = f'{date.today()}'
    future_date = f'{date.today() + timedelta(days=5)}'

    try:
        payload = json.loads(request_body)
        payload["request_id"] = request_id
        if "status" not in payload:
            pass
        elif "status" in payload["status"]:
            payload["status"] = "NEW"
        if "payment" in payload:
            if "guid" in payload["payment"]:
                payload["payment"]["guid"] = doc_guid
            if "date" in payload["payment"]:
                payload["payment"]["date"] = doc_date
            if "future_date" in payload["payment"]:
                payload["payment"]["future_date"] = future_date
        if "kicked_key" in payload:
            del payload["kicked_key"]

# сортируем ключи запроса так, как нам удобно их видеть, для читаемости конечного файла
        beauty_json = OrderedDict(payload)
        actual_keys = beauty_json.keys()
        preferred_keys = ['additional_docs', 'payee', 'payer', 'payment', 'party_flag', 'type', 'personal_id',
                          'third_flag', 'second_flag', 'first_flag', 'status', 'channel', 'request_id']
        for key in preferred_keys:
            if key in actual_keys:
                beauty_json.move_to_end(key, last=False)
        return beauty_json
    except json.JSONDecodeError as e:
        print(f'Возникла проблема при парсинге json в тест кейсе {case_key}, проверьте синтаксис тела запроса.', e)


# определяет эндпоинт, исходя из содержимого тела запроса
# можно искать иные ключи в какой-то другой части тест-кейса, например, искать в имени кейса название метода
# и таким образом формировать запрос
def edit_endpoint(payload):
    global endpoint_for_uat_json
    if payload["channel"] == "ABC" and special_endpoint_flag is True:
        endpoint = "endpoint/special"
    elif ("type" in payload and payload["type"] == "vpp") or (payload["channel"] == "DEF"):
        endpoint = "v1/endpoint"
    elif payload["channel"] == "GHI" or payload["second_flag"] == "VIP_CLIENT":
        endpoint = "endpoin/vip"
    else:
        endpoint = "standart/endpoint"
    path = f"{url}/{endpoint}"
    endpoint_for_uat_json = endpoint
    return path


# Можно заложить автоподмену плательщика
def payload_part_change(payload):
    payload.update({'payer': {'bank': {'name': 'БАНК ВТБ (ПАО)', 'corrAcc': '30101810700000000187',
                                       'bic': '044525187'}, 'typeClient': 'outer', 'accountNumber': '40702810001030000001'}})


# вызывает бьютификатор тела запроса, модифицирует тело, если в названии кейса есть триггер - "Внешний плательщик",
# вызывает функцию определения эндпоинта по телу запроса, формирует хэдеры и делает запрос с полученным из кейса телом.
def do_request(request_body, case_name, case_key):
    payload = beautify_request_body(request_body, case_key)
    path = edit_endpoint(payload)
    if 'Внешний плательщик' in case_name:
        payload_part_change(payload)
    payload_dump = json.dumps(payload)
    headers = {
        'Content-Type': 'application/json'
    }
    print(f'{case_key}. Эндпоинт и тело запроса:\n'
          f'{path}\n'
          f'{payload_dump}\n')
    res = requests.request("POST", path, verify=False, headers=headers, data=payload_dump)
    return payload, res.json()


# Собирает все в одно целое. На вход поступает двумерный массив(инфа о кейсах).
# Каждый массив 2 уровня содержит [case_name, case_request_body, case_folder, case_coverage, case_key].
# Пишем в файл информацию о тест-кейсе, тело запроса, тело ответа. Если такой файл уже есть, он перезапишется.
def create_uat_data(parsed_data):
    if os.path.exists(rest_file):
        open(rest_file, 'w').close()
    number = 1
    for x in parsed_data:
        payload, response = do_request(x[1], x[0], x[4])
        case_name, folder, coverage, case_key = x[0], x[2], x[3], x[4]
        with open(rest_file, 'a', encoding='utf-8') as file:
            file.writelines('=' * 120 + '\n'
                                        f'Кейс {number}.\n'
                                        f'Стори: {coverage}\n'
                                        f'Папка тест-кейса: {folder}\n'
                                        f'Номер и название кейса: {case_key} {case_name}\n'
                                        f'Отправить на эндпоинт {endpoint_for_uat_json} следующее тело запроса:\n')
            json.dump(payload, file, indent=4, ensure_ascii=False)
            file.writelines(f'\n\nТело ответа:\n')
            json.dump(response, file, indent=4, ensure_ascii=False)
            file.write('\n\n\n\n')
            file.close()
        number += 1
    print(f'Файл для ПСИ {rest_file} создан.')


# cтартуем процесс
create_uat_data(parse_xlsx())
