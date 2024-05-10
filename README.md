A simple python controller for distribution of extra solar power to change my tesla.

It uses modbus solaredge intf to read current power production, calculates sliding 30 sec avg and decides on how much energy should be diverted to a tesla charge.

It then calls tesla APIs to start or stop charge, as well as set the charge current to a value so all of electricity goes from solar.

If current battery is less than 50% it charges on max current ( 16A )

If current battery is more than 79% is stops charge.

If current solar production leaves less than 5A to spare on charge, the charging is stopped ( although technically tesla can accept less than 5A charge current, it makes little sence to keep all the systems powered for so little )

It also makes some effort to make less calls to tesla APIs which lead to car wake up 
to minimizes the idle energy drain of the car. The current sun position, geo position of the car and current battery state heuristics are used for it.

To turn off management, set charge limit to anything but not 50% or 79% ( eg if you just to override this logic and make a fast charge or not charge at all ).
