import os

# Get board serial
sn = raw_input("Enter baord serial:\t")

while sn != "0000":
    pcb_sn = raw_input("Enter pcb serial:\t")
    
    #read file as list
    f = open("board" + sn + ".txt", 'r')
    content = f.readlines()
    f.close()
    
    #look for marker line in file
    foundLine = False
    for index, line in enumerate(content):
        if line[0:12] == "Board model:":
            foundLine = True
            linePos = index
            break
    if foundLine:
        content[linePos + 1:linePos + 1] = "PCB serial: " + pcb_sn + '\n'
        f = open("board" + sn + ".txt", 'w')
        f.writelines(content)
        f.close()
    else:
        print("Could not find line to append to for board " + sn)
        
    sn = raw_input("Enter baord serial:\t")