--
-- Remove table if it already exists..
--
DROP TABLE IF EXISTS blog.my_network_devices ;

--
-- Create new table. Note that we don't specify what's in 'stuff_we_search' and
-- stuff_we_dont_search
CREATE TABLE blog.my_network_devices (
  device_id TEXT NOT NULL,
  reading_timestamp TIMESTAMP NOT NULL,
  ip TEXT NOT NULL,
  mac TEXT NOT NULL,
  reported_location OBJECT(STRICT) AS(lat DOUBLE PRECISION,long DOUBLE PRECISION),
  stuff_we_search OBJECT(DYNAMIC),
  stuff_we_dont_search OBJECT(IGNORED),
  PRIMARY KEY (device_id,reading_timestamp)
);

--
-- See what our table looks like.
--
SHOW CREATE TABLE blog.my_network_devices;

--
-- Insert a row.
INSERT INTO blog.my_network_devices
  (device_id, reading_timestamp, ip, mac, reported_location, stuff_we_search, stuff_we_dont_search)
VALUES (
  '38U10M57C03110',
  NOW(),
  '10.13.1.1',
  'D8:EC:5E:8E:ED:9E',
  {lat = 48.1374, long = 11.5755},
  {
    name = 'Router',
    description = 'Velop AX4200 WiFi 6 System',
    manufacturer = 'Linksys',
    model_number = 'MX42-EU',
    fw_ver = '1.0.13.216903',
    hw_version = '48SAQB11.0GA',
    serial_number = '38U10M57C03110'
  },
  {
    extra_macs = ['de:ec:5e:8e:ed:9f', 'd8:ec:5e:8e:ed:a1', 'd8:ec:5e:8e:ed:a0',
                  'da:ec:5e:8e:ed:a2', 'e6:ec:5e:8e:ed:9f', 'd8:ec:5e:8e:ed:9e',
                  'e2:ec:5e:8e:ed:9f', 'd8:ec:5e:8e:ed:9f', 'de:ec:5e:8e:ed:a0'],
    "userAp2G_bssid" = 'D8:EC:5E:8E:ED:9F',
    "userAp2G_channel" = '13'
  }
);

--
-- See what our table looks like now...
--
SHOW CREATE TABLE blog.my_network_devices;

