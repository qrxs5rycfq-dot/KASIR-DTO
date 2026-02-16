"""
USB Thermal Printer Module for Server-Side Printing
This provides consistent printing that doesn't depend on browser Bluetooth.
"""

import os
import json
from datetime import datetime

# Try to import escpos, but don't fail if not installed
try:
    from escpos.printer import Usb, Network
    from escpos.exceptions import USBNotFoundError
    ESCPOS_AVAILABLE = True
except ImportError:
    ESCPOS_AVAILABLE = False
    USBNotFoundError = Exception

# Try to import usb for device detection
try:
    import usb.core
    import usb.util
    USB_AVAILABLE = True
except ImportError:
    USB_AVAILABLE = False


class USBPrinterManager:
    """Manages USB thermal printer connections"""
    
    # Common thermal printer vendor/product IDs
    KNOWN_PRINTERS = [
        {'vendor_id': 0x0416, 'product_id': 0x5011, 'name': 'Generic Thermal Printer'},
        {'vendor_id': 0x04B8, 'product_id': 0x0202, 'name': 'Epson TM-T20'},
        {'vendor_id': 0x04B8, 'product_id': 0x0E15, 'name': 'Epson TM-T88'},
        {'vendor_id': 0x0519, 'product_id': 0x0001, 'name': 'Star TSP100'},
        {'vendor_id': 0x0483, 'product_id': 0x5720, 'name': 'POS-58/80'},
        {'vendor_id': 0x1504, 'product_id': 0x0006, 'name': 'POS Printer'},
        {'vendor_id': 0x0FE6, 'product_id': 0x811E, 'name': 'Thermal Printer'},
        {'vendor_id': 0x6868, 'product_id': 0x0200, 'name': 'XPrinter'},
        {'vendor_id': 0x0DD4, 'product_id': 0x0001, 'name': 'Custom Thermal'},
    ]
    
    def __init__(self):
        self.printer = None
        self.connected = False
        self.printer_info = None
    
    @staticmethod
    def is_available():
        """Check if USB printing is available on this system"""
        return ESCPOS_AVAILABLE and USB_AVAILABLE
    
    @staticmethod
    def list_usb_devices():
        """List all connected USB devices that might be printers"""
        if not USB_AVAILABLE:
            return []
        
        devices = []
        try:
            for dev in usb.core.find(find_all=True):
                # Check if it matches known printers
                for known in USBPrinterManager.KNOWN_PRINTERS:
                    if dev.idVendor == known['vendor_id'] and dev.idProduct == known['product_id']:
                        devices.append({
                            'vendor_id': hex(dev.idVendor),
                            'product_id': hex(dev.idProduct),
                            'name': known['name'],
                            'manufacturer': getattr(dev, 'manufacturer', 'Unknown'),
                            'product': getattr(dev, 'product', 'Unknown')
                        })
                        break
                else:
                    # Check if device class indicates printer (7 = Printer)
                    if dev.bDeviceClass == 7 or any(
                        cfg.bInterfaceClass == 7 
                        for cfg in dev 
                        for intf in cfg
                    ):
                        devices.append({
                            'vendor_id': hex(dev.idVendor),
                            'product_id': hex(dev.idProduct),
                            'name': 'USB Printer',
                            'manufacturer': getattr(dev, 'manufacturer', 'Unknown'),
                            'product': getattr(dev, 'product', 'Unknown')
                        })
        except Exception as e:
            print(f"Error listing USB devices: {e}")
        
        return devices
    
    def connect(self, vendor_id=None, product_id=None):
        """Connect to USB printer"""
        if not ESCPOS_AVAILABLE:
            return False, "python-escpos not installed"
        
        try:
            if vendor_id and product_id:
                # Connect to specific printer
                vid = int(vendor_id, 16) if isinstance(vendor_id, str) else vendor_id
                pid = int(product_id, 16) if isinstance(product_id, str) else product_id
                self.printer = Usb(vid, pid)
            else:
                # Auto-detect first available printer
                for known in self.KNOWN_PRINTERS:
                    try:
                        self.printer = Usb(known['vendor_id'], known['product_id'])
                        self.printer_info = known
                        break
                    except USBNotFoundError:
                        continue
                
                if not self.printer:
                    return False, "No USB printer found"
            
            self.connected = True
            return True, "Connected successfully"
            
        except USBNotFoundError:
            return False, "USB printer not found"
        except Exception as e:
            return False, str(e)
    
    def disconnect(self):
        """Disconnect from printer"""
        if self.printer:
            try:
                self.printer.close()
            except:
                pass
        self.printer = None
        self.connected = False
    
    def print_receipt(self, order_data):
        """Print a receipt from order data"""
        if not self.connected or not self.printer:
            return False, "Printer not connected"
        
        try:
            p = self.printer
            
            # Initialize
            p.set(align='center', bold=True)
            
            # Header
            p.text("================================\n")
            p.text(" DAPOER TERAS OBOR \n")
            p.text("    Kuliner Nusantara\n")
            p.text("================================\n")
            
            p.set(align='center', bold=False)
            p.text("Jl. Rw. Belong, Jakarta Barat\n")
            p.text("Telp: 021-XXXXXXX\n\n")
            
            # Order info
            p.set(align='left')
            p.text(f"No Order : {order_data.get('order_number', 'N/A')}\n")
            p.text(f"Tanggal  : {datetime.now().strftime('%d/%m/%Y')}\n")
            p.text(f"Jam      : {datetime.now().strftime('%H:%M:%S')}\n")
            
            if order_data.get('table'):
                p.text(f"Meja     : {order_data['table']}\n")
            if order_data.get('customer_name'):
                p.text(f"Nama     : {order_data['customer_name']}\n")
            
            p.text("--------------------------------\n")
            p.set(align='center', bold=True)
            p.text("DETAIL PESANAN\n")
            p.set(align='left', bold=False)
            p.text("--------------------------------\n")
            
            # Items
            for item in order_data.get('items', []):
                name = item.get('name', '')[:20]
                qty = item.get('quantity', 1)
                subtotal = item.get('subtotal', 0)
                p.text(f"{name}\n")
                p.text(f"   x{qty}           Rp {subtotal:,.0f}\n")
            
            p.text("--------------------------------\n")
            
            # Totals
            subtotal = order_data.get('subtotal', 0)
            tax = order_data.get('tax', 0)
            discount = order_data.get('discount', 0)
            total = order_data.get('total', 0)
            
            p.text(f"Subtotal        Rp {subtotal:,.0f}\n")
            if tax > 0:
                p.text(f"Pajak (10%)     Rp {tax:,.0f}\n")
            if discount > 0:
                p.text(f"Diskon          Rp -{discount:,.0f}\n")
            
            p.set(bold=True)
            p.text(f"TOTAL           Rp {total:,.0f}\n")
            p.set(bold=False)
            
            # Payment info
            payment_method = order_data.get('payment_method', 'cash')
            cash_amount = order_data.get('cash_amount', total)
            change = order_data.get('change', 0)
            
            p.text("--------------------------------\n")
            p.text(f"Bayar ({payment_method.upper()})\n")
            p.text(f"                Rp {cash_amount:,.0f}\n")
            if change > 0:
                p.text(f"Kembalian       Rp {change:,.0f}\n")
            
            # Footer
            p.text("\n")
            p.set(align='center')
            p.text("--------------------------------\n")
            p.text("Terima Kasih\n")
            p.text("Selamat Menikmati!\n")
            p.text("--------------------------------\n")
            
            # Cut paper
            p.cut()
            
            return True, "Receipt printed successfully"
            
        except Exception as e:
            return False, str(e)
    
    def print_raw(self, commands):
        """Print raw ESC/POS commands"""
        if not self.connected or not self.printer:
            return False, "Printer not connected"
        
        try:
            if isinstance(commands, list):
                self.printer._raw(bytes(commands))
            elif isinstance(commands, str):
                commands_list = json.loads(commands)
                self.printer._raw(bytes(commands_list))
            else:
                self.printer._raw(commands)
            
            return True, "Printed successfully"
        except Exception as e:
            return False, str(e)
    
    def test_print(self):
        """Print a test page"""
        if not self.connected or not self.printer:
            return False, "Printer not connected"
        
        try:
            p = self.printer
            
            p.set(align='center', bold=True)
            p.text("=== TEST PRINT ===\n")
            p.set(bold=False)
            p.text("USB Printer\n")
            p.text("Dapoer Teras Obor\n")
            p.text(f"{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n")
            p.text("Printer berfungsi dengan baik!\n\n")
            p.cut()
            
            return True, "Test print successful"
        except Exception as e:
            return False, str(e)


# Global instance for server-side printing
usb_printer = USBPrinterManager()
