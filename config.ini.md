[printer]
# IP-адрес Canon iR2425 в сети
ip =192.168.0.99

# Имя принтера точно как в ОС
# Windows: Панель управления → Устройства и принтеры → имя принтера
# Linux:   lpstat -p
name = 

[print]
# Режим брошюры: none | left | right
booklet = none

# Страниц в одной пачке (64 стр = 32 листа при дуплексе)
batch_size = 64

# Минут охлаждения между пачками
cooldown_minutes = 15

# Дуплекс: long | short | none
duplex = short

# Количество копий по умолчанию
copies = 1

# На сколько частей (чанков) делить одну пачку при отправке по Wi-Fi
chunks_per_batch = 2

[snmp]
community = public
port = 161
timeout = 3
retries = 2

# Секунд ожидания выхода из idle (cold start до 6 мин)
wake_timeout_sec = 600

# Минут watchdog на одну пачку
print_watchdog_min = 120

[telegram]
token = 

chat_id = 
download_timeout = 300

[logging]
file_level    = DEBUG
console_level = INFO
