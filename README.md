To admit on line one this code is heavily AI Assisted. I am not very knowledgeable in python and wanted to use this as a learning experience and fun project.
-----------------------

Code Referenced and taken from these sources.

https://github.com/LumaTeam/Luma3DS

https://github.com/TuxSH/InputRedirectionClient-Qt

https://github.com/PretendoNetwork

If any of these sources have issue with how ive used their code please contact me and Ill make the required changes or actions.
------------------------
This project is an implementation of InputRedirect-QT to work locally on a Rasperry Pi Pico 2W to allow for wireless game controller inputs to the 3DS family of systems without a MITM device.
------------------------

Setup
------------------------
Flash a Pi Pico 2W with the latest version of Micropython and add the main.py to the pico

In the main.py you can change the name for the access point that the Pico provides to avoid issues with multiple devices

On the 3ds connect via wifi to your Pi Picos AP and enable LumaCFW Input Redirect

Take note of the IP Input redirect gives you, if it is different from the expected IP in the pico change it accordingly on the main.py

Pinout
-----------------------

GP2	A

GP3	B

GP4	Select

GP5	Start

GP6	D-pad

GP7	D-pad

GP8	D-pad Up

GP9	D-pad Down

GP10	R

GP11	L

GP12	X

GP13	Y

GP14	Circle Pad Up

GP15	Circle Pad Down

GP16	Circle Pad Left

GP17	Circle Pad Right

GP18	ZLnNew 3DS only

GP19	ZR New 3DS only

GP20	HOME	

GP21	POWER

GP22	POWER (long-press)

GP26 (ADC0)	C-stick X	Analog New 3DS only

GP27 (ADC1)	C-stick Y	Analog New 3DS only

GP28 UNUSED

GP23, 24, 25, 29	— reserved —	n/a	used internally by the CYW43 wireless chip


Circle pad is all digital for the fact that there are not enough analog pins without an external ADC which this code could easily be adapted to use and create a full dual analog controller

Known issues
-----------------------

As input redirect does not send Cstick inputs to the OLD 3DS systems this as well cannot send them. Do not expect this to add real CPP functionality to an OLD 3DS System (3ds/2ds/3dsxl)

Due to how Input redirect in LumaCFW is written pressing ZL/ZR on the console will disable the use of the C-stick on the pico.
As well as pressing L/R on the pico disables all face buttons/Dpad on console when pressed/held.

Obviously this is not ideal as many games use the face buttons and L/R for actions.
If there were a way around this Id love to know.
