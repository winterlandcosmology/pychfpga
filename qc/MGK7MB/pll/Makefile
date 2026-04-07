default: pllprog

clean:
	rm -f *.o pllprog

pllprog: ftdispi.o pllprog.o
	gcc -o $@ $^ -lftdi
