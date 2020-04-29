import robust
import machine
import ubinascii
import time
import dht
import onewire, ds18x20
import ujson
import sys
import network
import math

import homie

config = {
"esp32" : False,
"client_id" : b"esp_" + ubinascii.hexlify(network.WLAN().config('mac')),
"broker" : "192.168.2.25",
"location" : "",
"dht" : False,
"ds1820" : False,
"bme280" : False,
"analog_period" : 0,
"analog_period2" : 0,
"debug" : False
}

class ColorManager:
    def __init__(self, pwms):
        self.pwms = pwms
        self.cycle = 0
        self.angles = [0,0,0]
        self.increments = [math.pi/180, math.pi/260, math.pi/225]

    def set_color(self, topic, value):
        for pwm, val in zip(self.pwms, value.split(",")):
            pwm.duty(int(float(val)*1023/255))
        return True

    def do_cycle(self):
        if self.cycle:
            for angle, pwm in zip(self.angles, self.pwms):
                pwm.duty(int(511*math.cos(angle)+511))
            self.angles = [ (angle + inc)%(math.pi*2) for angle, inc in zip(self.angles, self.increments) ]

    def set_cycler(self, topic, value):
        self.cycle = int(value)
        return True


class Dimmer(homie.Property):

    def __init__(self, dim_id, pwm, bt_pin):
        super(Dimmer, self).__init__("chan_"+dim_id.lower(), "Dimmer "+dim_id.upper(), "float", "%", "0:100", 0, self.set_value)
        self.pwm = pwm
        self.button = machine.Pin(bt_pin, machine.Pin.IN, machine.Pin.PULL_UP)
        self.time_cnt = 0
        self.last_value = 0
        self.delta = 25
        self.top_pause = 0

    def periodic(self):
        """Short press (less than 10 calls) switches on and off, long press (more than 10 calls)
        increases brightness (every 2 calls) and when at maximum decrease brightness to zero while button is pressed.

        Buttons are active low, hardware uses internall ESP pull up for open position.
        """
        if self.button.value() == 0:
            if self.time_cnt > 10 and self.time_cnt % 2 == 0 and self.top_pause == 0:
                self.time_cnt += 1
                self.pwm.duty( self.pwm.duty() + self.delta )
                if self.pwm.duty() == 0:
                    self.delta = -self.delta
                elif self.pwm.duty() == 1023:
                    self.delta = -self.delta
                    self.top_pause = 15
                print(self.pwm.duty(), self.delta)
                self.send_value(str(self.pwm.duty()/1023))
            else:
                self.time_cnt += 1
                if self.top_pause:
                    self.top_pause -= 1
        elif 0 < self.time_cnt <= 10:
            self.top_pause = 0
            if self.pwm.duty() == 0:
                self.pwm.duty(self.last_value)
            else:
                self.last_value = self.pwm.duty()
                self.pwm.duty(0)
            self.send_value(str(self.pwm.duty()/1023))
            self.time_cnt = 0
        else:
            self.time_cnt = 0

    def set_value(self, topic, value):
        self.pwm.duty(int(float(value)*1023))
        return True

class Analog(homie.Property):
    def __init__(self, prop_id, prop_name, pin, period):
        super(Analog, self).__init__(prop_id, prop_name, "float", None, "0:1", 0)
        if config["esp32"]:
            pin = machine.Pin(pin)
        self.adc = machine.ADC(pin)
        self.period = period

    def periodic(self, cur_time):
        if (cur_time % self.period) == 0:
            self.send_value(str(self.adc.read()/1023.0))

def main_loop():

    try:
        with open("config.json", "rt") as cfg_file:
             config.update( ujson.loads( cfg_file.read() ) )
    except OSError:
        pass

    #create the pwm channels
    pwm0 = machine.PWM(machine.Pin(12), duty=0, freq=150)
    pwm1 = machine.PWM(machine.Pin(13), duty=0)
    if config["esp32"]:
        pwm2 = machine.PWM(machine.Pin(2), duty=0)
        pwm3 = machine.PWM(machine.Pin(4), duty=0)
    else:
        pwm2 = machine.PWM(machine.Pin(15), duty=0)

    color_manager = ColorManager([pwm0, pwm1, pwm2])

    #~ robust.MQTTClient.DEBUG = True

    #create the mqtt client using config parameters
    client = robust.MQTTClient(config["client_id"], config["broker"])
    client.connect()

    #create dht if it is enabled in config
    if config["dht"]:
        temp_sensor = dht.DHT22(machine.Pin(0))
    elif config["ds1820"]:
        temp_sensor = ds18x20.DS18X20(onewire.OneWire(machine.Pin(0)))
        rom_id = temp_sensor.scan()[0]
    elif config["bme280"]:
        import bme280
        i2c = machine.I2C(0, scl=machine.Pin(22), sda=machine.Pin(21))
        temp_sensor = bme280.BME280(i2c=i2c,address=0x76)
    else:
        temp_sensor = None

    adcs = []
    #check in config for analog period
    analog_period = config['analog_period']
    analog_period2 = config['analog_period2']
    if config["esp32"]:
        if analog_period != 0:
            adcs.append(Analog("analog1", "Analog sensor 1", 34, analog_period))
        if analog_period2 != 0:
            adcs.append(Analog("analog2", "Analog sensor 2", 35, analog_period2))
    else:
        if analog_period != 0:
            adcs.append(Analog("analog1", "Analog sensor 1", 0, analog_period))


    #create the buttons and pwm channels

    if config["esp32"]:
        dimmers = [ Dimmer("A", pwm0, 32), Dimmer("B", pwm1, 33),
                    Dimmer("C", pwm2, 25), Dimmer("D", pwm3, 26) ]
    else:
        dimmers = [ Dimmer("A", pwm0, 4), Dimmer("B", pwm1, 5), Dimmer("C", pwm2, 14) ]

    props_color = [ homie.Property("color", "desired color RGB", "color", None, "rgb", "000,000,000", color_manager.set_color),
                    homie.Property("cycler", "cycler mode", "integer", None, None, "0", color_manager.set_cycler) ]

    env_props = []
    if config["dht"] or config["ds1820"] or config["bme280"]:
        env_props.append(homie.Property("temperature", "Temperature", "float", "°C".encode("utf-8"), None, 0))
    if config["dht"] or config["bme280"]:
        env_props.append(homie.Property("humidity", "Humidity", "float", "%", "0:100", 0))
    if config["bme280"]:
         env_props.append(homie.Property("pressure", "Atmospheric pressure", "float", "mBar", None, 0))

    nodes = [ homie.Node("color", "Color leds (on ABC)", props_color), homie.Node("dimmer", "Dimmers channels", dimmers)]

    if env_props:
        nodes.append(homie.Node("evironment", "Environment Measures", env_props))

    if adcs:
        nodes.append(homie.Node("analog_sens", "Analog Sensors", adcs))

    device = homie.HomieDevice( client, ubinascii.hexlify(network.WLAN().config('mac')), nodes, "Multicontroler{}".format(config["location"]))

    time_tmp = 0

    dht_retry = 0

    dht_err_ctr = 0

    try:
        while True:
            device.main()
            time.sleep(.050)

            cur_time = int(time.time())

            color_manager.do_cycle()

            #this simple test enables to get in this loop every second
            if time_tmp != cur_time:
                #~ print("In the loop")
                time_tmp = cur_time

                #tempeature is read from dht periodically
                if config["dht"] and (cur_time % 60) == dht_retry :
                    try:
                        temp_sensor.measure()
                    except (OSError, dht.DHTChecsumError) as excp:
                        #If we have an exception, retry in 4 seconds
                        dht_retry +=4
                        dht_err_ctr += 1
                        if dht_retry == 40:
                            #too many retries, raise the original exception
                            raise
                        else:
                            #logging
                            print("DHT error, retrying")
                    else:
                        dht_retry = 0
                        dht_err_ctr = 0
                        #all went well let's publish temperature
                        env_props[0].send_value(str(temp_sensor.temperature()))
                        print(temp_sensor.temperature())

                        #publish humidity
                        if (cur_time % (60*30)) == dht_retry:
                            env_props[1].send_value(str(temp_sensor.humidity()))
                elif config["ds1820"] and (cur_time % 60) == 0:
                    temp_sensor.convert_temp()
                elif config["ds1820"] and (cur_time % 60) == 1:
                    temp = temp_sensor.read_temp(rom_id)
                    #all went well let's publish temperature
                    env_props[0].send_value('{:.1f}'.format(temp))
                    print(temp)
                elif config["bme280"] and (cur_time % 60) == 0:
                    temp,pa,hum = temp_sensor.read_compensated_data()
                    env_props[0].send_value('{:.1f}'.format(temp/100))
                    env_props[1].send_value(str(hum//1024))
                    env_props[2].send_value('{:.2f}'.format(pa/25600))
                    print (temp/100,pa//25600,hum/1024)
                    print(temp_sensor.values)

                #analog publication period is user defined
                for adc in adcs:
                    adc.periodic(cur_time)

            #buttons checks
            for dimmer in dimmers:
                dimmer.periodic()

    except KeyboardInterrupt as excp:
        print ("Interrupted")
        sys.print_exception(excp)

    except Exception as excp:
        sys.print_exception(excp)

        #if debug mode is not enabled in config automatic reset in 10 seconds
        if config['debug'] == False:
            for count in range (10,0, -1):
                print("Reboot in {} seconds\r".format(count))
                time.sleep(1)
            machine.reset()
    finally:
        client.disconnect()

main_loop()
